# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from ctypes import Array as CArray
from ctypes import c_wchar
from datetime import datetime
from multiprocessing.sharedctypes import RawArray
from ssl import SSLContext, TLSVersion, create_default_context
from typing import Any, ClassVar, NoReturn

import aiohttp
from wazo_auth_client.types import TokenDict

from .config import is_unix_socket
from .exception import (
    AuthenticationError,
    AuthenticationExpiredError,
    AuthServerUnavailableError,
    AuthServerUnreachableError,
)
from .helpers.timing import Cooldown, parse_expiration, utcnow_naive

DEFAULT_ACL = 'websocketd'
BROKER_RESPONSE_HEADER = 'X-Wazo-Websocketd-Broker'
_WAIT_FACTOR = 1.5  # must be > 1
_LOOPBACK_ADDRESS_TABLE = {'0.0.0.0': '127.0.0.1', '::': '::1', '*': '127.0.0.1'}

logger = logging.getLogger(__name__)
_TokenGetter = Callable[[], dict[str, Any]]
_LOOPBACK_HOSTS = {'localhost', '127.0.0.1', '::1'}


def _build_ssl(verify_certificate: bool | str) -> bool | SSLContext | None:
    if isinstance(verify_certificate, str):
        context = create_default_context(cafile=verify_certificate)
        context.minimum_version = TLSVersion.TLSv1_2
        return context
    return None if verify_certificate else False


class AsyncAuthClient:
    _ACL = DEFAULT_ACL
    _TIMEOUT = 10.0
    _CONNECT_TIMEOUT = 2.0

    def __init__(self, config: dict[str, Any]) -> None:
        host = config.get('host', 'localhost')
        self._socket_path = host if is_unix_socket(host) else None
        self._base_url = self._build_endpoint(config)
        self._ssl = (
            None
            if self._socket_path
            else _build_ssl(config.get('verify_certificate', True))
        )
        self._credentials = None
        if username := config.get('username'):
            self._credentials = aiohttp.BasicAuth(
                username, config.get('password') or ''
            )

        self._timeout: float = config.get('timeout') or self._TIMEOUT
        self._session: aiohttp.ClientSession | None = None

    def _build_endpoint(self, config: dict[str, Any]) -> str:
        prefix = config.get('prefix') or ''
        if prefix and not prefix.startswith('/'):
            prefix = f'/{prefix}'

        if self._socket_path:
            return f'http://localhost{prefix}/0.1'

        host = config.get('host', 'localhost')
        if not config.get('https') and host not in _LOOPBACK_HOSTS:
            logger.warning(
                'talking to wazo-auth on %s in clear text: set `auth.https`', host
            )

        # bracket ipv6 literals, which a url authority requires
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'

        scheme = 'https' if config.get('https') else 'http'
        return f'{scheme}://{host}:{config["port"]}{prefix}/0.1'

    async def get_token(self, token_id: str, acl: str | None = None) -> TokenDict:
        logger.debug('retrieving token data and validating authorization')
        return await self._send(
            'GET', f'/token/{token_id}', params={'scope': acl or self._ACL}
        )

    async def create_token(self, expiration: int) -> TokenDict:
        logger.debug('creating a service token')
        return await self._send('POST', '/token', json={'expiration': expiration})

    async def is_valid_token(self, token_id: str, acl: str | None = None) -> bool:
        logger.debug('checking token validity')
        try:
            await self._send(
                'HEAD', f'/token/{token_id}', params={'scope': acl or self._ACL}
            )
        except AuthenticationError:
            return False
        return True

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _classify_error(self, error: aiohttp.ClientResponseError) -> NoReturn:
        if 400 <= error.status < 500:
            raise AuthenticationError(error, status_code=error.status)
        raise AuthServerUnavailableError(error)

    async def _send(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            async with self.session.request(
                method,
                f'{self._base_url}{path}',
                ssl=self._ssl,
                auth=self._credentials,
                **kwargs,
            ) as response:
                response.raise_for_status()
                if response.status == 204:
                    return None
                return (await response.json())['data']
        except aiohttp.ClientResponseError as e:
            self._classify_error(e)
        except (TimeoutError, aiohttp.ClientError) as e:
            raise AuthServerUnreachableError(e)
        except (json.JSONDecodeError, KeyError) as e:
            raise AuthServerUnavailableError(f'malformed response: {e}')

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = (
                aiohttp.UnixConnector(path=self._socket_path)
                if self._socket_path
                else aiohttp.TCPConnector()
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(
                    total=self._timeout, connect=self._CONNECT_TIMEOUT
                ),
            )
        return self._session


class FallbackAuthClient(AsyncAuthClient):
    """Reads through this server, falling back to another when unreachable."""

    def __init__(
        self,
        config: dict[str, Any],
        fallback: AsyncAuthClient,
        *,
        unreachable_cooldown: float = 5.0,
    ) -> None:
        super().__init__(config)
        self._fallback = fallback
        self._unreachable_cooldown = unreachable_cooldown
        self._breaker: Cooldown | None = None

    async def create_token(self, expiration: int) -> TokenDict:
        return await self._fallback.create_token(expiration)

    async def close(self) -> None:
        await super().close()
        await self._fallback.close()

    def _classify_error(self, error: aiohttp.ClientResponseError) -> NoReturn:
        if BROKER_RESPONSE_HEADER not in (error.headers or {}):
            raise AuthServerUnreachableError(error)
        super()._classify_error(error)

    async def _send(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._breaker is not None and not self._breaker.expired:
            return await self._fallback._send(method, path, **kwargs)

        try:
            answer = await super()._send(method, path, **kwargs)
        except AuthServerUnreachableError as e:
            self._open_breaker(e)
            return await self._fallback._send(method, path, **kwargs)

        self._close_breaker()
        return answer

    def _open_breaker(self, error: Exception) -> None:
        if self._breaker is None:
            logger.warning(
                '%s unreachable, reading from the next one for %gs at a time: %s',
                self._base_url,
                self._unreachable_cooldown,
                error,
            )
            self._breaker = Cooldown(self._unreachable_cooldown)
        else:
            self._breaker.restart()

    def _close_breaker(self) -> None:
        if self._breaker is not None:
            logger.info('%s is answering again', self._base_url)
            self._breaker = None


class _AuthChecker(ABC):
    strategy: ClassVar[str]
    _strategies: ClassVar[dict[str, type[_AuthChecker]]] = {}

    def __init__(self, client: AsyncAuthClient, config: dict[str, Any]) -> None:
        self._auth_client = client
        self._max_unavailable = config['auth_check_max_unavailable']

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if strategy := getattr(cls, 'strategy', None):
            _AuthChecker._strategies[strategy] = cls

    @classmethod
    def get_strategy(cls, strategy: str) -> type[_AuthChecker]:
        if strategy not in cls._strategies:
            raise ValueError(
                f'unknown auth_check_strategy `{strategy}`, '
                f'expected one of {sorted(cls._strategies)}'
            )
        return cls._strategies[strategy]

    @abstractmethod
    def _next_delay(self, token: dict[str, Any]) -> float:
        ...

    @abstractmethod
    async def _check(self, token_id: str) -> None:
        """Raise AuthenticationExpiredError once the token no longer holds."""

    async def run(self, token_getter: _TokenGetter) -> None:
        # one instance serves every session, so the tally stays local
        unavailable = 0
        while True:
            await asyncio.sleep(self._next_delay(token_getter()))
            logger.debug('%s auth check: testing token validity', self.strategy)
            try:
                await self._check(token_getter()['token'])
            except AuthServerUnavailableError as e:
                unavailable += 1
                logger.warning(
                    '%s auth check: wazo-auth unavailable '
                    '(%d/%d consecutive failures)',
                    self.strategy,
                    unavailable,
                    self._max_unavailable,
                )
                if unavailable >= self._max_unavailable:
                    raise AuthServerUnavailableError() from e
                continue
            unavailable = 0


class _StaticIntervalAuthChecker(_AuthChecker):
    strategy = 'static'

    def __init__(self, client: AsyncAuthClient, config: dict[str, Any]) -> None:
        super().__init__(client, config)
        self._interval = config['auth_check_static_interval']

    def _next_delay(self, _token: dict[str, Any]) -> float:
        return self._interval

    async def _check(self, token_id: str) -> None:
        if not await self._auth_client.is_valid_token(token_id):
            raise AuthenticationExpiredError()


class _DynamicIntervalAuthChecker(_AuthChecker):
    strategy = 'dynamic'

    def _next_delay(self, token: dict[str, Any]) -> float:
        expires_at = parse_expiration(token['utc_expires_at'])
        return self._calculate_next_check(utcnow_naive(), expires_at)

    async def _check(self, token_id: str) -> None:
        try:
            await self._auth_client.get_token(token_id)
        except AuthenticationError as e:
            raise AuthenticationExpiredError() from e

    def _calculate_next_check(self, now: datetime, expires_at: datetime) -> float:
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


class Authenticator:
    def __init__(self, client: AsyncAuthClient, auth_check: _AuthChecker) -> None:
        self._auth_client = client
        self._auth_check = auth_check

    def get_token(self, token_id: str) -> Awaitable[TokenDict]:
        return self._auth_client.get_token(token_id)

    def run_check(self, token_getter: _TokenGetter) -> Awaitable[None]:
        # This function returns a coroutine that raise an AuthenticationExpiredError exception
        # when the token expires.
        return self._auth_check.run(token_getter)

    async def close(self) -> None:
        await self._auth_client.close()

    async def __aenter__(self) -> Authenticator:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()


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
    def matches(cls, tenant_uuid: str) -> bool:
        return cls.has_master_tenant() and cls.proxy.value == tenant_uuid

    @classmethod
    def has_master_tenant(cls) -> bool:
        # an unset shared buffer reads as an empty string, never as None
        return bool(cls.proxy.value)


def _broker_address(broker: dict[str, Any]) -> str | None:
    if connect := broker.get('connect'):
        return connect

    listen: str | None = broker.get('listen')
    if not listen:
        return None
    return _LOOPBACK_ADDRESS_TABLE.get(listen, listen)


def build_auth_client(config: dict[str, Any]) -> AsyncAuthClient:
    direct = AsyncAuthClient(config['auth'])
    broker = config.get('broker') or {}
    if address := _broker_address(broker):
        timeout = config['auth']['timeout'] * _WAIT_FACTOR
        return FallbackAuthClient(
            {**broker, 'host': address, 'timeout': timeout}, direct
        )
    return direct


def build_authenticator(config: dict[str, Any]) -> Authenticator:
    client = build_auth_client(config)
    checker = _AuthChecker.get_strategy(config['auth_check_strategy'])
    return Authenticator(client, checker(client, config))
