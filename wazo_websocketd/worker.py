# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from signal import SIGINT, SIGTERM

from setproctitle import setproctitle
from websockets.legacy.server import WebSocketServer, WebSocketServerProtocol
from xivo.xivo_logging import setup_logging, silence_loggers

from .auth import Authenticator, MasterTenantProxy, StringSharedBuffer
from .bus import BusService
from .exception import ControlChannelClosed, HandoffError
from .ipc import (
    Handoff,
    Heartbeat,
    Revoke,
    WebSocketHandler,
    WorkerWebSocketServer,
    adopt_connection,
    drain_pending_handoffs,
    receive_connection,
)
from .protocol import SessionProtocolDecoder, SessionProtocolEncoder
from .session import SessionFactory

HandlerFactory = Callable[[dict], WebSocketHandler]

logger = logging.getLogger(__name__)


class _RevokingAuthenticator(Authenticator):
    def __init__(self, config: dict, control_sock: socket.socket) -> None:
        super().__init__(config)
        self._control_sock = control_sock

    def invalidate(self, token_id: str) -> None:
        try:
            self._control_sock.send(Revoke(token_id).marshal())
        except OSError:
            logger.warning(
                'failed to signal token revocation to supervisor; '
                'revocation will not be cached until the token expires'
            )


class Worker:
    def __init__(
        self,
        control_sock: socket.socket,
        handler_factory: HandlerFactory,
        ws_server: WebSocketServer,
        *,
        heartbeat_interval: float = 1.0,
        drain_timeout: float = 30.0,
    ) -> None:
        control_sock.setblocking(False)
        self._control_sock = control_sock
        self._handler_factory = handler_factory
        self._ws_server = ws_server
        self._heartbeat_interval = heartbeat_interval
        self._drain_timeout = drain_timeout
        self._connections: set[WebSocketServerProtocol] = set()
        self._stopping = asyncio.Event()

    @classmethod
    def entrypoint(
        cls,
        config: dict,
        control_sock: socket.socket,
        master_tenant_proxy: StringSharedBuffer,
        name: str,
    ) -> None:
        setproctitle(f'wazo-websocketd: {name}')
        setup_logging(
            config['log_file'], debug=config['debug'], log_level=config['log_level']
        )
        silence_loggers(['aioamqp', 'urllib3', 'stevedore.extension'], logging.WARNING)

        MasterTenantProxy.proxy = master_tenant_proxy
        authenticator = _RevokingAuthenticator(config, control_sock)
        service = BusService(config)

        factory = SessionFactory(
            config,
            authenticator,
            service,
            SessionProtocolEncoder(),
            SessionProtocolDecoder(),
        )
        worker = cls(control_sock, factory.make_handler, WorkerWebSocketServer())
        asyncio.run(worker._serve(service))

    async def _serve(self, service: BusService) -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(SIGINT, self.stop)
        loop.add_signal_handler(SIGTERM, self.stop)

        async with service:
            await self._run()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        heartbeat_task = self._loop.create_task(self._heartbeat_loop())
        try:
            await self._accept_loop()
            await self._drain()
        finally:
            heartbeat_task.cancel()

    def stop(self) -> None:
        self._ws_server.start_draining()
        self._stopping.set()

    async def _accept_loop(self) -> None:
        stopping = self._loop.create_task(self._stopping.wait())
        try:
            while not self._stopping.is_set():
                receiving = self._loop.create_task(
                    receive_connection(self._loop, self._control_sock)
                )
                try:
                    done, _pending = await asyncio.wait(
                        {receiving, stopping}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    if not receiving.done():
                        receiving.cancel()

                if receiving not in done:
                    break

                try:
                    sock, payload = receiving.result()
                except ControlChannelClosed:
                    logger.info('control channel closed, worker stopping')
                    break
                except (HandoffError, OSError):
                    logger.exception('failed to receive a handed-off connection')
                    continue

                try:
                    await self._adopt(sock, payload)
                except Exception:
                    logger.exception('failed to adopt a handed-off connection')
                    sock.close()
        finally:
            stopping.cancel()

        for sock, payload in drain_pending_handoffs(self._control_sock):
            try:
                await self._adopt(sock, payload)
            except Exception:
                logger.exception('failed to adopt a buffered handoff during drain')
                sock.close()

    async def _adopt(self, sock: socket.socket, payload: bytes) -> None:
        handoff = Handoff.unpack(payload)
        handler = self._handler_factory(handoff.token)
        protocol = await adopt_connection(
            self._loop, sock, handoff.request, handler, self._ws_server
        )
        self._connections.add(protocol)
        protocol.handler_task.add_done_callback(
            lambda _task, p=protocol: self._connections.discard(p)
        )

    async def _drain(self) -> None:
        if not self._connections:
            return

        protocols = list(self._connections)
        await asyncio.gather(
            *(p.close(1001, 'server shutting down') for p in protocols),
            return_exceptions=True,
        )

        pending = [p.handler_task for p in protocols if not p.handler_task.done()]
        if pending:
            _done, still_running = await asyncio.wait(
                pending, timeout=self._drain_timeout
            )
            for task in still_running:
                task.cancel()

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            self.send_heartbeat()

    def send_heartbeat(self) -> None:
        try:
            self._control_sock.send(Heartbeat(len(self._connections)).marshal())
        except (BlockingIOError, OSError):
            logger.debug('heartbeat send skipped')
