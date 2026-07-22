# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from collections.abc import Callable
from uuid import UUID

from cachetools import TLRUCache

from .auth import TokenProvider, parse_expiration, utcnow_naive
from .exception import AuthenticationError, AuthServerUnavailableError

logger = logging.getLogger(__name__)

_MASKED_UUID = 'XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX'


def mask_uuid(value: UUID | str) -> str:
    return _MASKED_UUID[:-8] + str(value)[-8:]


def _token_size(token: dict) -> int:
    return len(json.dumps(token).encode())


class CachingAuthenticator:
    def __init__(
        self,
        authenticator: TokenProvider,
        *,
        positive_ttl: float,
        negative_ttl: float,
        max_size: int,
        max_negative_entries: int,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._authenticator = authenticator
        self._positive_ttl = positive_ttl
        self._negative_ttl = negative_ttl
        self._positive: TLRUCache[str, dict] = TLRUCache(
            maxsize=max_size, ttu=self._positive_ttu, timer=timer, getsizeof=_token_size
        )
        self._negative: TLRUCache[str, bool] = TLRUCache(
            maxsize=max_negative_entries, ttu=self._negative_ttu, timer=timer
        )
        self._inflight: dict[str, asyncio.Task] = {}

    async def get_token(self, token_id: str) -> dict:
        masked = mask_uuid(token_id)
        if (token := self._positive.get(token_id)) is not None:
            logger.debug('token cache: positive hit `%s`', masked)
            return token

        if self._negative.get(token_id):
            logger.debug('token cache: negative hit `%s`', masked)
            raise AuthenticationError('token previously rejected (cached)')

        task = self._inflight.get(token_id)
        if task is None:
            logger.debug('token cache: miss `%s`, querying wazo-auth', masked)
            task = asyncio.create_task(self._fetch(token_id))
            self._inflight[token_id] = task
            task.add_done_callback(functools.partial(self._discard_inflight, token_id))
        else:
            logger.debug('token cache: coalescing in-flight lookup `%s`', masked)

        return await task

    def invalidate(self, token_id: str) -> None:
        logger.debug('token cache: invalidating `%s`', mask_uuid(token_id))
        self._positive.pop(token_id, None)
        self._negative[token_id] = True

    def _discard_inflight(self, token_id: str, _task: asyncio.Task) -> None:
        self._inflight.pop(token_id, None)

    async def _fetch(self, token_id: str) -> dict:
        masked = mask_uuid(token_id)
        try:
            token = await self._authenticator.get_token(token_id)
        except AuthenticationError:
            logger.debug('token cache: caching rejection `%s`', masked)
            self._negative[token_id] = True  # hard reject: safe to cache
            self._positive.pop(token_id, None)
            raise
        except AuthServerUnavailableError:
            logger.debug('token cache: auth unavailable `%s`, not cached', masked)
            raise  # soft failure: never cache an outage as a rejection

        if self._seconds_until_expiry(token) > 0:
            try:
                self._positive[token_id] = token
            except ValueError:
                logger.debug(
                    'token cache: token `%s` exceeds budget, not cached', masked
                )
            else:
                logger.debug('token cache: storing token `%s`', masked)
                self._negative.pop(token_id, None)

        return token

    def _positive_ttu(self, _key: str, token: dict, now: float) -> float:
        return now + min(self._positive_ttl, self._seconds_until_expiry(token))

    def _negative_ttu(self, _key: str, _value: bool, now: float) -> float:
        return now + self._negative_ttl

    def _seconds_until_expiry(self, token: dict) -> float:
        try:
            expires_at = parse_expiration(token['utc_expires_at'])
        except (KeyError, ValueError, TypeError):
            return self._positive_ttl
        return (expires_at - utcnow_naive()).total_seconds()
