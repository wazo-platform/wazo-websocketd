# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from collections.abc import Awaitable, Callable
from math import inf
from typing import Protocol
from uuid import UUID

from cachetools import TLRUCache, TTLCache

from .exception import AuthenticationError, AuthServerUnavailableError
from .helpers.timing import parse_expiration, utcnow_naive

logger = logging.getLogger(__name__)

_CacheKey = tuple[str, 'str | None']

_MASKED_UUID = 'XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX'


class TokenProvider(Protocol):
    def get_token(self, token_id: str, acl: str | None = None) -> Awaitable[dict]:
        ...


def mask_uuid(value: UUID | str) -> str:
    return _MASKED_UUID[:-8] + str(value)[-8:]


def _token_size(token: dict) -> int:
    return len(json.dumps(token).encode())


class CachingAuthenticator:
    # TTL for new positive entries while the bus eviction channel is down:
    # a dropped session-deleted event must not extend staleness past this
    DEGRADED_TTL = 30.0

    # a rejection is only cached long enough to collapse a retry storm: wazo-auth
    # can start accepting a token it just refused (replication lag, ACL granted)
    NEGATIVE_TTL_CEILING = 5.0

    # what wazo-auth answers for the token of a session it deleted
    REVOKED_STATUS = 404

    # a lookup already in flight when the deletion arrives can still be answered
    # 200 by a wazo-auth that has not committed it yet: outlast that window
    DELETED_SESSION_MEMORY = 5.0

    # validation is unauthenticated: shed load rather than relay a flood of
    # cache misses to wazo-auth
    MAX_INFLIGHT_LOOKUPS = 500

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
        self._degraded = False
        self._positive: TLRUCache[_CacheKey, dict] = TLRUCache(
            maxsize=max_size, ttu=self._positive_ttu, timer=timer, getsizeof=_token_size
        )
        self._negative: TLRUCache[_CacheKey, int] = TLRUCache(
            maxsize=max_negative_entries, ttu=self._negative_ttu, timer=timer
        )
        self._deletions: TTLCache[str, bool] = TTLCache(
            maxsize=max_negative_entries, ttl=self.DELETED_SESSION_MEMORY, timer=timer
        )
        self._by_session: dict[str, set[_CacheKey]] = {}
        self._inflight: dict[_CacheKey, asyncio.Task] = {}

    async def get_token(self, token_id: str, acl: str | None = None) -> dict:
        key = (token_id, acl)
        masked = mask_uuid(token_id)
        if (token := self._positive.get(key)) is not None:
            logger.debug('positive hit `%s` (acl=%s)', masked, acl)
            return token

        if status := self._negative.get(key):
            logger.debug('negative hit `%s` (acl=%s)', masked, acl)
            raise AuthenticationError(
                'token previously rejected (cached)', status_code=status
            )

        task = self._inflight.get(key)
        if task is None:
            if len(self._inflight) >= self.MAX_INFLIGHT_LOOKUPS:
                logger.warning(
                    'refusing `%s` (acl=%s), %d lookups already in flight',
                    masked,
                    acl,
                    len(self._inflight),
                )
                raise AuthServerUnavailableError('too many lookups in flight')

            logger.debug('miss `%s` (acl=%s), querying wazo-auth', masked, acl)
            task = asyncio.create_task(self._fetch(key))
            self._inflight[key] = task
            task.add_done_callback(functools.partial(self._discard_inflight, key))
        else:
            logger.debug('coalescing in-flight lookup `%s` (acl=%s)', masked, acl)

        return await asyncio.shield(task)

    def evict_session(self, session_uuid: str) -> None:
        self._deletions[session_uuid] = True
        keys = self._by_session.pop(session_uuid, set())
        if not keys:
            return
        logger.info(
            'session `%s` deleted: refusing %d cache entrie(s)',
            mask_uuid(session_uuid),
            len(keys),
        )
        for key in keys:
            self._positive.pop(key, None)
            self._negative[key] = self.REVOKED_STATUS

    def eviction_active(self) -> None:
        self._set_degraded(False)

    def eviction_lost(self) -> None:
        self._set_degraded(True)

    def _set_degraded(self, degraded: bool) -> None:
        if degraded == self._degraded:
            return

        logger.info(
            'positive cache TTL %s',
            f'degraded to {self.DEGRADED_TTL:.0f}s' if degraded else 'restored',
        )
        self._degraded = degraded
        self._flush_positive()

    def _flush_positive(self) -> None:
        logger.info('flushing %d positive cache entries', len(self._positive))
        self._positive.clear()
        self._by_session.clear()

    async def stop(self) -> None:
        lookups = list(self._inflight.values())
        for task in lookups:
            task.cancel()
        await asyncio.gather(*lookups, return_exceptions=True)
        self._inflight.clear()  # the done callbacks have not run yet

    def positive_entries(self) -> int:
        return len(self._positive)

    def inflight_lookups(self) -> int:
        return len(self._inflight)

    def is_degraded(self) -> bool:
        return self._degraded

    def _discard_inflight(self, key: _CacheKey, _task: asyncio.Task) -> None:
        self._inflight.pop(key, None)

    async def _fetch(self, key: _CacheKey) -> dict:
        token_id, acl = key
        masked = mask_uuid(token_id)
        try:
            token = await self._authenticator.get_token(token_id, acl)
        except AuthenticationError as e:
            logger.debug('caching rejection `%s` (acl=%s)', masked, acl)
            self._negative[key] = e.status_code  # hard reject: safe to cache
            self._positive.pop(key, None)
            raise
        except AuthServerUnavailableError:
            logger.debug('auth unavailable `%s`, not cached', masked)
            raise  # soft failure: never cache an outage as a rejection

        if token.get('session_uuid') in self._deletions:
            logger.info('token `%s` belongs to a deleted session, refusing it', masked)
            self._negative[key] = self.REVOKED_STATUS
            raise AuthenticationError(
                'session was deleted', status_code=self.REVOKED_STATUS
            )

        if self._seconds_until_expiry(token) <= 0:
            logger.debug('token `%s` already expired, not cached', masked)
            return token

        try:
            self._positive[key] = token
        except ValueError:
            logger.debug('token `%s` exceeds budget, not cached', masked)
        else:
            logger.debug('storing token `%s` (acl=%s)', masked, acl)
            self._negative.pop(key, None)
            self._remember_session(key, token)

        return token

    def _remember_session(self, key: _CacheKey, token: dict) -> None:
        if (session_uuid := token.get('session_uuid')) is None:
            return

        self._prune_sessions()
        self._by_session.setdefault(session_uuid, set()).add(key)

    def _prune_sessions(self) -> None:
        # entries expire on their own schedule, so the index only learns of it here
        if len(self._by_session) <= max(len(self._positive), 1) * 2:
            return

        logger.debug('pruning the session index (%d entries)', len(self._by_session))
        for session_uuid, keys in list(self._by_session.items()):
            keys = {key for key in keys if key in self._positive}
            if keys:
                self._by_session[session_uuid] = keys
            else:
                del self._by_session[session_uuid]

    def _positive_ttu(self, _key: _CacheKey, token: dict, now: float) -> float:
        ttl = min(self._positive_ttl, self.DEGRADED_TTL if self._degraded else inf)
        return now + min(ttl, self._seconds_until_expiry(token))

    def _negative_ttu(self, _key: _CacheKey, _value: int, now: float) -> float:
        return now + min(self._negative_ttl, self.NEGATIVE_TTL_CEILING)

    def _seconds_until_expiry(self, token: dict) -> float:
        try:
            expires_at = parse_expiration(token['utc_expires_at'])
        except (KeyError, ValueError, TypeError):
            return self._positive_ttl
        return (expires_at - utcnow_naive()).total_seconds()
