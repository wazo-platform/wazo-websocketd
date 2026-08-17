# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from secrets import token_hex
from signal import SIGINT, SIGTERM

from aioamqp.exceptions import AmqpClosedConnection, ChannelClosed
from aiohttp import web

from .auth import DEFAULT_ACL, AsyncAuthClient
from .bus import SESSION_DELETED_EVENT, BusConnection, bus_url, parse_event_session_uuid
from .config import is_unix_socket, load_config
from .exception import (
    AuthenticationError,
    AuthServerUnavailableError,
    BusConnectionError,
)
from .helpers.process import bootstrap, child_identity, drop_privileges
from .status import BROKER_ID, StatusServer, serve_unix
from .token_cache import CachingAuthenticator, mask_uuid

logger = logging.getLogger(__name__)


class SessionInvalidator:
    _RETRY_DELAY = 1.0

    def __init__(
        self,
        connection: BusConnection,
        config: dict,
        cache: CachingAuthenticator,
    ) -> None:
        self._connection = connection
        self._exchange = config['bus']['exchange_name']
        self._exchange_type = config['bus']['exchange_type']
        self._cache = cache
        self._lost = asyncio.Event()

    async def connection_lost(self) -> None:
        self._lost.set()

    async def run(self) -> None:
        while True:
            self._lost.clear()
            try:
                await self._subscribe()
            except (BusConnectionError, AmqpClosedConnection, ChannelClosed) as e:
                if self._connection.is_closing:
                    return
                logger.warning('could not subscribe to bus events: %s, retrying', e)
                await asyncio.sleep(self._RETRY_DELAY)
                continue

            self._cache.eviction_active()
            logger.info('bus session eviction active')

            await self._lost.wait()
            self._cache.eviction_lost()

    async def _subscribe(self) -> None:
        channel = await self._connection.get_channel()
        await channel.exchange(self._exchange, self._exchange_type, durable=True)
        response = await channel.queue(
            f'wazo-websocketd-broker-{token_hex(3)}',
            durable=False,
            auto_delete=True,
            exclusive=True,
        )
        queue = response['queue']
        await channel.queue_bind(
            queue,
            self._exchange,
            '',
            arguments={'x-match': 'all', 'name': SESSION_DELETED_EVENT},
        )
        await channel.basic_consume(self._on_event, queue, no_ack=True)

    async def _on_event(self, channel, body, envelope, properties) -> None:
        if (session_uuid := parse_event_session_uuid(body)) is not None:
            self._cache.evict_session(session_uuid)


class BrokerServer:
    def __init__(self, config: dict):
        self._config = config
        self._tasks: list[asyncio.Task] = []

        self._upstream_client = AsyncAuthClient(config['auth'])

        self._cache = CachingAuthenticator(
            self._upstream_client, **config['token_cache']
        )
        self._cache.eviction_lost()

        self._bus_connection = BusConnection(bus_url(config))
        self._invalidator = SessionInvalidator(
            self._bus_connection, config, self._cache
        )
        self._bus_connection.add_consumer(self._invalidator)

        listen = config['broker']['listen']
        if not listen:
            raise ValueError(
                'configuration key `broker.listen` is required to run the broker'
            )
        self._socket_path = listen if is_unix_socket(listen) else None

        name, supervised = child_identity(config, BROKER_ID)
        app = make_app(self._cache)
        self._runner = web.AppRunner(app, access_log=None)
        self._status_server = StatusServer(
            name=name,
            supervised=supervised,
            connection_counter=self._cache.positive_entries,
            host_app=app if self._socket_path else None,
            **config['status'],
        )

    @property
    def port(self) -> int | None:
        if self._socket_path is not None:
            return None
        return self._runner.addresses[0][1]

    async def start(self) -> None:
        broker_config = self._config['broker']
        await self._runner.setup()
        await self._bind(broker_config)

        loop = asyncio.get_event_loop()
        self._tasks = [
            loop.create_task(self._bus_connection.run()),
            loop.create_task(self._invalidator.run()),
        ]

        await self._status_server.start()

        logger.info(
            'listening on %s (upstream wazo-auth: %s:%s)',
            self._socket_path or '%s:%s' % self._runner.addresses[0][:2],
            self._config['auth'].get('host', 'localhost'),
            self._config['auth'].get('port', 9497),
        )

    async def _bind(self, broker_config: dict) -> None:
        if self._socket_path is None:
            site = web.TCPSite(
                self._runner, broker_config['listen'], broker_config['port']
            )
            await site.start()
            return

        await serve_unix(self._runner, self._socket_path)

    async def stop(self) -> None:
        await self._bus_connection.disconnect()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._status_server.stop()
        await self._runner.cleanup()
        await self._upstream_client.close()

        if self._socket_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(self._socket_path)

        logger.info('stopped')

    async def serve(self) -> None:
        tombstone = asyncio.Event()
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(SIGINT, tombstone.set)
        loop.add_signal_handler(SIGTERM, tombstone.set)
        await self.start()
        try:
            await tombstone.wait()
        finally:
            await self.stop()


def main() -> None:
    config = load_config()

    bootstrap(config, child_identity(config, BROKER_ID)[0])
    drop_privileges(config)
    asyncio.run(BrokerServer(config).serve())


def make_app(cache: CachingAuthenticator) -> web.Application:
    app = web.Application()
    app['token_cache'] = cache
    app.add_routes(
        [
            web.head('/0.1/token/{token_id}', _head_token),
            web.get('/0.1/token/{token_id}', _get_token, allow_head=False),
        ]
    )
    return app


class _Rejection(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


async def _get_token(request: web.Request) -> web.Response:
    try:
        token = await _resolve(request)
    except _Rejection as e:
        return web.json_response(
            {'reason': [e.reason], 'status_code': e.status}, status=e.status
        )
    return web.json_response({'data': token})


async def _head_token(request: web.Request) -> web.Response:
    try:
        await _resolve(request)
    except _Rejection as e:
        return web.Response(status=e.status)
    return web.Response(status=204)


async def _resolve(request: web.Request) -> dict:
    token_id = request.match_info['token_id']
    acl = request.query.get('scope') or DEFAULT_ACL
    cache: CachingAuthenticator = request.app['token_cache']
    try:
        token = await cache.get_token(token_id, acl)
    except AuthenticationError as e:
        logger.debug('%s rejected (%d)', _describe(request, acl), e.status_code)
        raise _Rejection(e.status_code, str(e)) from e
    except AuthServerUnavailableError as e:
        logger.warning(
            'wazo-auth unavailable, %s answered 503', _describe(request, acl)
        )
        raise _Rejection(503, 'authentication server unavailable') from e
    logger.debug('%s served', _describe(request, acl))
    return token


def _describe(request: web.Request, acl: str) -> str:
    masked = mask_uuid(request.match_info['token_id'])
    return f'{request.method} token `{masked}` (scope={acl})'
