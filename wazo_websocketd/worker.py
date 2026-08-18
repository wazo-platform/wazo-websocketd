# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from enum import IntEnum
from os import getpid
from signal import SIGINT, SIGTERM
from socket import gethostname

from websockets.exceptions import InvalidMessage
from websockets.server import WebSocketServer, WebSocketServerProtocol, serve

from wazo_websocketd.auth import MasterTenant, build_authenticator
from wazo_websocketd.bus import BusService
from wazo_websocketd.config import load_config
from wazo_websocketd.helpers.process import bootstrap, child_identity, drop_privileges
from wazo_websocketd.protocol import SessionProtocolDecoder, SessionProtocolEncoder
from wazo_websocketd.session import CloseCode, SessionFactory
from wazo_websocketd.status import StatusServer
from wazo_websocketd.token_renewer import ServiceTokenRenewer

logger = logging.getLogger(__name__)

_DRAIN_WINDOW = 10.0


class ProbeTolerantProtocol(WebSocketServerProtocol):
    async def read_http_request(self):
        try:
            return await super().read_http_request()
        except InvalidMessage as exc:
            if isinstance(exc.__cause__, EOFError):
                # bare TCP probe (health check): the peer disconnected without
                # sending a request; ConnectionError makes websockets close the
                # transport silently instead of logging a handshake traceback
                raise ConnectionError('connection closed during handshake') from exc
            raise


async def force_close(session: WebSocketServerProtocol, after: float) -> None:
    await asyncio.sleep(after)
    await session.close(CloseCode.GOING_AWAY)


async def drain(ws_server: WebSocketServer, window: float = _DRAIN_WINDOW) -> None:
    # stop accepting: any remaining reuse_port group members keep the port
    ws_server.server.close()
    await ws_server.server.wait_closed()

    if sessions := list(ws_server.websockets):
        interval = window / len(sessions)
        logger.info(
            'closing %d connection(s) as going away, one every %.0fms',
            len(sessions),
            interval * 1000,
        )
        await asyncio.gather(
            *(
                force_close(session, after=position * interval)
                for position, session in enumerate(sessions)
            )
        )

    ws_server.close()
    await ws_server.wait_closed()


class WebsocketServer:
    def __init__(self, config: dict):
        self._config = config
        self._authenticator = build_authenticator(config)
        self._stack: AsyncExitStack | None = None
        self._ws_server: WebSocketServer | None = None

    def _create_server(self) -> tuple[BusService, WebSocketServer]:
        config = self._config
        service: BusService = BusService(config)
        factory: SessionFactory = SessionFactory(
            config,
            self._authenticator,
            service,
            SessionProtocolEncoder(),
            SessionProtocolDecoder(),
        )

        host = config['websocket']['listen']
        port = config['websocket']['port']
        ssl = config['websocket']['ssl']

        server = serve(
            factory.ws_handler,
            host=host,
            port=port,
            ssl=ssl,
            reuse_port=True,
            create_protocol=ProbeTolerantProtocol,
        )

        return service, server

    async def start(self):
        service, server = self._create_server()
        self._stack = AsyncExitStack()
        self._stack.push_async_callback(self._authenticator.close)
        await self._stack.enter_async_context(service)
        await service.initialize_exchanges()
        self._ws_server = await self._stack.enter_async_context(server)

    async def shutdown(self):
        if self._ws_server is not None:
            await drain(self._ws_server)
        if self._stack is not None:
            await self._stack.aclose()

    def connection_count(self) -> int:
        if self._ws_server is None:
            return 0
        return len(self._ws_server.websockets)


class WorkerExit(IntEnum):
    STOPPED = 0
    INIT_TIMEOUT = 1


class Worker:
    _INIT_TIMEOUT = 60.0

    def __init__(self, config: dict):
        self._config = config
        self._name, self._supervised = child_identity(config, gethostname())
        self._tombstone = asyncio.Event()

    @property
    def name(self) -> str:
        return self._name

    def request_stop(self) -> None:
        self._tombstone.set()

    async def run(self) -> WorkerExit:
        tombstone = self._tombstone
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(SIGINT, self.request_stop)
        loop.add_signal_handler(SIGTERM, self.request_stop)

        websocket_server = WebsocketServer(self._config)
        status_server = StatusServer(
            name=self._name,
            supervised=self._supervised,
            connection_counter=websocket_server.connection_count,
            ready_checker=MasterTenant.is_known,
            **self._config['status'],
        )

        logger.info('starting (pid %d)', getpid())
        async with AsyncExitStack() as stack:
            stack.push_async_callback(status_server.stop)
            await status_server.start()

            if not await self._initialize(stack, websocket_server):
                logger.error(
                    'could not become ready within %.0fs, giving up',
                    self._INIT_TIMEOUT,
                )
                return WorkerExit.INIT_TIMEOUT

            await tombstone.wait()
            status_server.set_draining()

        logger.info('stopped')
        return WorkerExit.STOPPED

    @asynccontextmanager
    async def _init_master_tenant(self) -> AsyncIterator[None]:
        if master_tenant := self._config.get('master_tenant'):
            MasterTenant.set_uuid(master_tenant)
            yield
            return

        renewer = ServiceTokenRenewer(self._config)
        renewer.subscribe(MasterTenant.set, details=True, oneshot=True)
        renewer.subscribe(renewer.stop, oneshot=True)
        async with renewer:
            yield

    async def _initialize(
        self, stack: AsyncExitStack, websocket_server: WebsocketServer
    ) -> bool:
        await stack.enter_async_context(self._init_master_tenant())

        stack.push_async_callback(websocket_server.shutdown)

        working = asyncio.ensure_future(websocket_server.start())
        stopping = asyncio.ensure_future(self._tombstone.wait())
        done, pending = await asyncio.wait(
            (working, stopping),
            timeout=self._INIT_TIMEOUT,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        if working in done:
            working.result()
            return True
        return stopping in done


def main() -> None:
    config = load_config()

    worker = Worker(config)
    bootstrap(config, worker.name)
    drop_privileges(config)

    exit_code = asyncio.run(worker.run())
    sys.exit(exit_code)
