# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from signal import SIGINT, SIGTERM
from typing import Any

from aiohttp import web
from aiohttp.typedefs import Handler

from .auth import BROKER_RESPONSE_HEADER, DEFAULT_ACL, AsyncAuthClient
from .bus import BusService
from .config import is_unix_socket, load_config
from .consumers import SessionInvalidator
from .exception import AuthenticationError, AuthServerUnavailableError
from .helpers.process import bootstrap, child_identity, drop_privileges
from .helpers.tasks import is_pending
from .status import BROKER_ID, StatusServer, serve_unix
from .token_cache import CachingAuthenticator, mask_uuid

logger = logging.getLogger(__name__)


class _TokenRefusedError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _HTTPApplication:
    def __init__(self, cache: CachingAuthenticator, name: str = BROKER_ID) -> None:
        self._cache = cache
        self._name = name

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._stamp_response])
        app.add_routes(
            [
                web.head('/0.1/token/{token_id}', self._head_token),
                web.get('/0.1/token/{token_id}', self._get_token, allow_head=False),
            ]
        )
        return app

    @web.middleware
    async def _stamp_response(
        self, request: web.Request, handler: Handler
    ) -> web.StreamResponse:
        try:
            response = await handler(request)
        except web.HTTPException as answer:
            answer.headers[BROKER_RESPONSE_HEADER] = self._name
            raise
        response.headers[BROKER_RESPONSE_HEADER] = self._name
        return response

    async def _get_token(self, request: web.Request) -> web.Response:
        try:
            token = await self._validate_token(request)
        except _TokenRefusedError as e:
            return web.json_response(
                {'reason': [e.reason], 'status_code': e.status}, status=e.status
            )
        return web.json_response({'data': token})

    async def _head_token(self, request: web.Request) -> web.Response:
        try:
            await self._validate_token(request)
        except _TokenRefusedError as e:
            return web.Response(status=e.status)
        return web.Response(status=204)

    async def _validate_token(self, request: web.Request) -> dict:
        token_id = request.match_info['token_id']
        acl = request.query.get('scope') or DEFAULT_ACL
        described = self._describe(request, acl)
        try:
            token = await self._cache.get_token(token_id, acl)
        except AuthenticationError as e:
            logger.debug('%s rejected (%d)', described, e.status_code)
            raise _TokenRefusedError(e.status_code, str(e)) from e
        except AuthServerUnavailableError as e:
            logger.warning('wazo-auth unavailable, %s answered 503', described)
            raise _TokenRefusedError(503, 'authentication server unavailable') from e
        logger.debug('%s served', described)
        return token

    @staticmethod
    def _describe(request: web.Request, acl: str) -> str:
        masked = mask_uuid(request.match_info['token_id'])
        return f'{request.method} token `{masked}` (scope={acl})'


class BrokerServer:
    def __init__(self, config: dict):
        self._config = config
        self._invalidator_task: asyncio.Task | None = None

        self._upstream_client = AsyncAuthClient(config['auth'])

        self._cache = CachingAuthenticator(
            self._upstream_client, **config['token_cache']
        )
        self._cache.eviction_lost()

        self._bus = BusService(config)
        self._invalidator = SessionInvalidator(
            self._bus.connection(), config, self._cache
        )

        if not config['broker']['listen']:
            raise ValueError(
                'configuration key `broker.listen` is required to run the broker'
            )
        self._socket_path: str | None = None

        name, supervised = child_identity(config, BROKER_ID)
        app = _HTTPApplication(self._cache, name).app()
        self._runner = web.AppRunner(app, access_log=None)
        self._status_server = StatusServer(
            name=name,
            supervised=supervised,
            ready_checker=self._is_ready,
            metrics=self._metrics,
            host_app=app if self.socket_path else None,
            **config['status'],
        )

    def _is_ready(self) -> bool:
        return not self._cache.is_degraded() and is_pending(self._invalidator_task)

    def _metrics(self) -> dict[str, Any]:
        return {
            'cached_tokens': self._cache.positive_entries(),
            'inflight_lookups': self._cache.inflight_lookups(),
        }

    @property
    def socket_path(self) -> str | None:
        listen = self._config['broker']['listen']
        return listen if is_unix_socket(listen) else None

    @property
    def port(self) -> int | None:
        if self.socket_path is not None:
            return None
        return self._runner.addresses[0][1]

    async def start(self) -> None:
        broker_config = self._config['broker']
        await self._runner.setup()
        await self._bind(broker_config)

        loop = asyncio.get_event_loop()
        await self._bus.start()
        self._invalidator_task = loop.create_task(self._invalidator.run())

        await self._status_server.start()

        logger.info(
            'listening on %s (upstream wazo-auth: %s:%s)',
            self.socket_path or '%s:%s' % self._runner.addresses[0][:2],
            self._config['auth'].get('host', 'localhost'),
            self._config['auth'].get('port', 9497),
        )

    async def _bind(self, broker_config: dict) -> None:
        socket_path = self.socket_path
        if socket_path is None:
            site = web.TCPSite(
                self._runner, broker_config['listen'], broker_config['port']
            )
            await site.start()
            return

        await serve_unix(self._runner, socket_path)
        self._socket_path = socket_path  # ours to unlink only once bound

    async def stop(self) -> None:
        await self._bus.stop()
        if self._invalidator_task is not None:
            self._invalidator_task.cancel()
            await asyncio.gather(self._invalidator_task, return_exceptions=True)
        await self._status_server.stop()
        await self._runner.cleanup()
        await self._cache.stop()
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
