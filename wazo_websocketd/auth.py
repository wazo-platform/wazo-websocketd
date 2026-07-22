# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import datetime
import logging
from abc import ABC, abstractmethod
from collections import namedtuple
from collections.abc import Awaitable, Callable
from ctypes import Array as CArray
from ctypes import c_wchar
from functools import partial
from itertools import chain, repeat
from multiprocessing.sharedctypes import RawArray
from typing import Protocol

import requests
from wazo_auth_client import Client as AuthClient
from wazo_auth_client.types import TokenDict

from .exception import (
    AuthenticationError,
    AuthenticationExpiredError,
    AuthServerUnavailableError,
)

logger = logging.getLogger(__name__)

_HARD_REJECT_STATUSES = (401, 403, 404)


def utcnow_naive() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def parse_expiration(value: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.UTC).replace(tzinfo=None)
    return parsed


class TokenProvider(Protocol):
    def get_token(self, token_id: str) -> Awaitable[dict]:
        ...


class TokenAuthenticator(TokenProvider, Protocol):
    def invalidate(self, token_id: str) -> None:
        ...


class AsyncAuthClient:
    _ACL = 'websocketd'

    def __init__(self, config):
        self._auth_client = AuthClient(**config['auth'])

    async def get_token(self, token_id):
        logger.debug(
            'retrieving token data from wazo-auth and validating authorization'
        )
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, self._auth_client.token.get, token_id, self._ACL
            )
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in _HARD_REJECT_STATUSES:
                raise AuthenticationError(e)
            raise AuthServerUnavailableError(e)
        except requests.RequestException as e:
            raise AuthServerUnavailableError(e)

    async def is_valid_token(self, token_id, acl=_ACL):
        logger.debug('checking token validity from wazo-auth')
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, self._auth_client.token.is_valid, token_id, acl
            )
        except requests.RequestException as e:
            raise AuthServerUnavailableError(e)


class _AuthChecker(ABC):
    @abstractmethod
    def __init__(self, async_auth_client: AsyncAuthClient, config: dict) -> None:
        ...

    @abstractmethod
    async def run(self, token_getter: Callable[[], dict]) -> None:
        ...


class _StaticIntervalAuthChecker(_AuthChecker):
    def __init__(self, async_auth_client, config):
        self._async_auth_client = async_auth_client
        self._interval = config['auth_check_static_interval']
        self._max_unavailable = config['auth_check_max_unavailable']

    async def run(self, token_getter):
        consecutive_unavailable = 0
        while True:
            await asyncio.sleep(self._interval)
            logger.debug('static auth check: testing token validity')
            token_id = token_getter()['token']
            try:
                is_valid = await self._async_auth_client.is_valid_token(token_id)
            except AuthServerUnavailableError:
                consecutive_unavailable += 1
                logger.warning(
                    'static auth check: wazo-auth unavailable '
                    '(%d/%d consecutive failures)',
                    consecutive_unavailable,
                    self._max_unavailable,
                )
                if consecutive_unavailable >= self._max_unavailable:
                    raise AuthServerUnavailableError()
                continue
            consecutive_unavailable = 0
            if not is_valid:
                raise AuthenticationExpiredError()


class _DynamicIntervalAuthChecker(_AuthChecker):
    def __init__(self, async_auth_client, config):
        self._async_auth_client = async_auth_client
        self._max_unavailable = config['auth_check_max_unavailable']

    async def run(self, token_getter):
        consecutive_unavailable = 0
        while True:
            token = token_getter()
            token_id = token['token']
            now = utcnow_naive()
            expires_at = parse_expiration(token['utc_expires_at'])
            next_check = self._calculate_next_check(now, expires_at)
            await asyncio.sleep(next_check)
            token = token_getter()
            token_id = token['token']
            logger.debug('dynamic auth check: testing token validity')
            try:
                await self._async_auth_client.get_token(token_id)
            except AuthServerUnavailableError:
                consecutive_unavailable += 1
                logger.warning(
                    'dynamic auth check: wazo-auth unavailable '
                    '(%d/%d consecutive failures)',
                    consecutive_unavailable,
                    self._max_unavailable,
                )
                if consecutive_unavailable >= self._max_unavailable:
                    raise AuthServerUnavailableError()
                continue
            except AuthenticationError:
                raise AuthenticationExpiredError()
            consecutive_unavailable = 0

    def _calculate_next_check(self, now, expires_at):
        delta = expires_at - now
        delta_seconds = delta.total_seconds()
        if delta_seconds <= 0:
            return 15
        elif delta_seconds < 15:
            return min(delta_seconds, 15)
        elif delta_seconds <= 80:
            return min(delta_seconds, 60)
        elif delta_seconds <= 57600:
            return int(0.75 * delta_seconds)
        return 43200


STRATEGIES = {
    'static': _StaticIntervalAuthChecker,
    'dynamic': _DynamicIntervalAuthChecker,
}


class Authenticator:
    def __init__(self, config):
        self._async_auth_client = AsyncAuthClient(config)
        auth_check_class: type[_AuthChecker] | None = STRATEGIES.get(
            config['auth_check_strategy']
        )
        if not auth_check_class:
            raise Exception(
                'unknown auth_check_strategy {}'.format(config['auth_check_strategy'])
            )
        self._auth_check = auth_check_class(self._async_auth_client, config)

    def get_token(self, token_id):
        # This function returns a coroutine.
        return self._async_auth_client.get_token(token_id)

    def is_valid_token(self, token_id, acl=None):
        # This function returns a coroutine.
        return self._async_auth_client.is_valid_token(token_id, acl)

    def run_check(self, token_getter):
        # This function returns a coroutine that raise an AuthenticationExpiredError exception
        # when the token expires.
        return self._auth_check.run(token_getter)


StringSharedBuffer = CArray[c_wchar]


class MasterTenantProxy:
    proxy: StringSharedBuffer = RawArray(c_wchar, 36)

    @classmethod
    def set_master_tenant(cls, token: TokenDict):
        try:
            tenant_uuid = token['metadata']['tenant_uuid']
        except KeyError:
            logger.error('invalid token, contains no tenant_uuid')
        else:
            logger.info('setting master_tenant_uuid to \'%s\'', tenant_uuid)
            cls.proxy.value = tenant_uuid

    @classmethod
    def get_master_tenant(cls) -> str | None:
        return cls.proxy.value

    @classmethod
    def has_master_tenant(cls) -> bool:
        return bool(cls.proxy.value)


class ServiceTokenRenewer:
    DEFAULT_EXPIRATION = 21600  # 6h
    DEFAULT_LEEWAY_FACTOR = 0.85

    Callback = namedtuple('Callback', ['method', 'details', 'oneshot'])

    def __init__(self, config: dict):
        self._callbacks: list[ServiceTokenRenewer.Callback] = []
        self._client = AuthClient(**config['auth'])
        self._expiration: int = self.DEFAULT_EXPIRATION
        self._lock = asyncio.Lock()
        self._loop = asyncio.get_event_loop()
        self._task: asyncio.Task = None  # type: ignore[assignment]

    async def __aenter__(self):
        logger.info('service token renewer started')
        self._task = self._loop.create_task(self._run())
        return self

    async def __aexit__(self, *args):
        if not self._task.cancelled():
            self._task.cancel()
        logger.info('service token renewer stopped')

    def subscribe(
        self,
        callback: Callable[[str], None],
        *,
        details: bool = False,
        oneshot: bool = False,
    ) -> None:
        callback_ = self.Callback(callback, details, oneshot)
        self._callbacks.append(callback_)

    def unsubscribe(
        self,
        callback: Callable[[str], None],
        *,
        details: bool = False,
        oneshot: bool = False,
    ) -> None:
        callback_ = self.Callback(callback, details, oneshot)
        try:
            self._callbacks.remove(callback_)
        except ValueError:
            pass

    async def _run(self):
        while True:
            token = await self._fetch_token()
            await self._notify(token)
            await asyncio.sleep(self._expiration * self.DEFAULT_LEEWAY_FACTOR)

    async def _fetch_token(self) -> TokenDict:
        timeouts = chain((1, 2, 4, 8, 16), repeat(32))
        fn = partial(self._client.token.new, expiration=self._expiration)
        while True:
            try:
                return await self._loop.run_in_executor(None, fn)
            except Exception:
                interval = next(timeouts)
                await self.on_error(interval)
            await asyncio.sleep(interval)

    async def _notify(self, token: TokenDict):
        callbacks = self._callbacks.copy()
        for callback in callbacks:
            if callback.oneshot:
                async with self._lock:
                    self._callbacks.remove(callback)
            payload = token if callback.details else token['token']
            self._loop.call_soon(callback.method, payload)

    async def on_error(self, interval: int):
        logger.error(
            'Failed to create an access token, retrying in %d seconds',
            interval,
        )
