# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from ssl import SSLContext, create_default_context
from typing import Any, ClassVar

import aiohttp
from wazo_auth_client.types import TokenDict

from .config import is_unix_socket
from .exception import (
    AuthenticationError,
    AuthenticationExpiredError,
    AuthServerUnavailableError,
)
from .helpers.timing import parse_expiration, utcnow_naive

DEFAULT_ACL = 'websocketd'
_SERVER_ERROR = 500

logger = logging.getLogger(__name__)
_TokenGetter = Callable[[], dict[str, Any]]


def _build_ssl(verify_certificate: bool | str) -> bool | SSLContext | None:
    if isinstance(verify_certificate, str):
        return create_default_context(cafile=verify_certificate)
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

    @staticmethod
    def _build_endpoint(config: dict[str, Any]) -> str:
        host = config.get('host', 'localhost')
        prefix = config.get('prefix') or ''
        if prefix and not prefix.startswith('/'):
            prefix = f'/{prefix}'

        if is_unix_socket(host):
            return f'http://localhost{prefix}/0.1'

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
            if e.status < _SERVER_ERROR:
                raise AuthenticationError(e, status_code=e.status)
            raise AuthServerUnavailableError(e)
        except (TimeoutError, aiohttp.ClientError) as e:
            raise AuthServerUnavailableError(e)

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

    def __init__(self, config: dict[str, Any], fallback: AsyncAuthClient) -> None:
        super().__init__(config)
        self._fallback = fallback

    async def create_token(self, expiration: int) -> TokenDict:
        return await self._fallback.create_token(expiration)

    async def close(self) -> None:
        await super().close()
        await self._fallback.close()

    async def _send(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            return await super()._send(method, path, **kwargs)
        except AuthServerUnavailableError as e:
            logger.warning(
                '%s unreachable, falling back to the next one: %s', self._base_url, e
            )
            return await self._fallback._send(method, path, **kwargs)


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
        # This function returns a coroutine.
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


class MasterTenant:
    _value: str | None = None

    @classmethod
    def set(cls, token: TokenDict) -> None:
        try:
            tenant_uuid = token['metadata']['tenant_uuid']
        except KeyError:
            logger.error('invalid token, contains no tenant_uuid')
        else:
            logger.info('setting master_tenant_uuid to \'%s\'', tenant_uuid)
            cls._value = tenant_uuid

    @classmethod
    def set_uuid(cls, tenant_uuid: str) -> None:
        logger.info('master_tenant_uuid supplied as \'%s\'', tenant_uuid)
        cls._value = tenant_uuid

    @classmethod
    def matches(cls, tenant_uuid: str) -> bool:
        return cls._value is not None and cls._value == tenant_uuid

    @classmethod
    def is_known(cls) -> bool:
        return cls._value is not None


def build_auth_client(config: dict[str, Any]) -> AsyncAuthClient:
    direct = AsyncAuthClient(config['auth'])
    broker = config.get('broker') or {}
    if address := broker.get('connect') or broker.get('listen'):
        return FallbackAuthClient({**broker, 'host': address}, direct)
    return direct


def build_authenticator(config: dict[str, Any]) -> Authenticator:
    client = build_auth_client(config)
    checker = _AuthChecker.get_strategy(config['auth_check_strategy'])
    return Authenticator(client, checker(client, config))
