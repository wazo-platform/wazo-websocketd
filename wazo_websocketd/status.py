# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import aiohttp
from aiohttp import web

from .helpers.timing import Cooldown

logger = logging.getLogger(__name__)

RUN_DIR = '/run/wazo-websocketd'
BROKER_ID = 'broker'
STATUS_LISTEN = '0.0.0.0'
STATUS_PORT = 9504
FETCH_TIMEOUT = 2.0


class WorkerState(StrEnum):
    STARTING = 'starting'
    READY = 'ready'
    DRAINING = 'draining'


async def socket_peer_pid(socket_path: str) -> int | None:
    """Pid of the process listening on `socket_path`, or None if nobody is."""
    credentials = struct.Struct('3i')
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(socket_path), FETCH_TIMEOUT
        )
    except (OSError, TimeoutError):
        return None

    try:
        client = writer.get_extra_info('socket')
        packed = client.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, credentials.size
        )
    except OSError:
        return None
    finally:
        writer.close()
        with contextlib.suppress(OSError, ConnectionError):
            await writer.wait_closed()

    pid, _, _ = credentials.unpack(packed)
    return pid or None


async def serve_unix(runner: web.AppRunner, socket_path: str) -> None:
    await remove_stale_socket(socket_path)
    await web.UnixSite(runner, socket_path).start()
    os.chmod(socket_path, 0o600)


async def _read_status(session: aiohttp.ClientSession) -> dict | None:
    try:
        async with session.get('http://localhost/status') as response:
            return await response.json()
    except (TimeoutError, OSError, aiohttp.ClientError, ValueError):
        return None


@dataclass
class _PolledSocket:
    session: aiohttp.ClientSession
    idle: Cooldown


class StatusClient:
    def __init__(self, *, idle_timeout: float, timeout: float = FETCH_TIMEOUT) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._idle_timeout = idle_timeout
        self._sockets: dict[str, _PolledSocket] = {}

    async def fetch(self, socket_path: str) -> dict | None:
        await self._close_idle(except_for=socket_path)
        status = await _read_status(self._session_for(socket_path))
        if status is None:
            await self.forget(socket_path)  # the socket may be gone for good
        return status

    async def forget(self, socket_path: str) -> None:
        polled = self._sockets.pop(socket_path, None)
        if polled is not None and not polled.session.closed:
            await polled.session.close()

    async def close(self) -> None:
        for socket_path in list(self._sockets):
            await self.forget(socket_path)

    def _session_for(self, socket_path: str) -> aiohttp.ClientSession:
        polled = self._sockets.get(socket_path)
        if polled is None or polled.session.closed:
            polled = _PolledSocket(
                session=aiohttp.ClientSession(
                    connector=aiohttp.UnixConnector(path=socket_path),
                    timeout=self._timeout,
                ),
                idle=Cooldown(self._idle_timeout),
            )
            self._sockets[socket_path] = polled
        else:
            polled.idle.restart()
        return polled.session

    async def _close_idle(self, *, except_for: str) -> None:
        for socket_path, polled in list(self._sockets.items()):
            if socket_path != except_for and polled.idle.expired:
                logger.debug('%s went unpolled, closing its session', socket_path)
                await self.forget(socket_path)


async def remove_stale_socket(socket_path: str) -> None:
    if not os.path.exists(socket_path):
        return
    if (owner := await socket_peer_pid(socket_path)) is not None:
        raise OSError(f'{socket_path} is already served by pid {owner}')
    logger.info('removing stale socket %s', socket_path)
    with contextlib.suppress(OSError):
        os.unlink(socket_path)


class StatusServer:
    def __init__(
        self,
        *,
        name: str,
        supervised: bool,
        connection_counter: Callable[[], int],
        ready_checker: Callable[[], bool] | None = None,
        host_app: web.Application | None = None,
        listen: str = STATUS_LISTEN,
        port: int = STATUS_PORT,
    ) -> None:
        self._name = name
        self._supervised = supervised
        self._connection_counter = connection_counter
        self._ready_checker = ready_checker
        self._listen = listen
        self._port = port
        self._draining = False
        self._socket_path: str | None = None
        self._runner: web.AppRunner | None = None

        if (app := host_app) is None:
            app = web.Application()
            self._runner = web.AppRunner(app, access_log=None)
        app.add_routes([web.get('/status', self._status)])

    @property
    def socket_path(self) -> str:
        return f'{RUN_DIR}/{self._name}.sock'

    @property
    def port(self) -> int | None:
        if self._runner is None or not self._runner.addresses:
            return None
        return self._runner.addresses[0][1]

    def set_draining(self) -> None:
        logger.info('now reporting as draining')
        self._draining = True

    async def start(self) -> None:
        if self._runner is None:  # a host app already serves our routes
            return
        await self._runner.setup()
        if self._supervised:
            socket_path = self.socket_path
            await serve_unix(self._runner, socket_path)
            self._socket_path = socket_path  # ours to unlink only once bound
            logger.info('status endpoint listening on %s', socket_path)
        else:
            await web.TCPSite(self._runner, self._listen, self._port).start()
            logger.info(
                'status endpoint listening on %s:%d', *self._runner.addresses[0]
            )

    async def stop(self) -> None:
        if self._runner is None:
            return
        await self._runner.cleanup()
        if self._socket_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(self._socket_path)

    async def _status(self, request: web.Request) -> web.Response:
        state = self._state()
        return web.json_response(
            {
                'state': state.value,
                'connections': self._connection_counter(),
                'name': self._name,
                'pid': os.getpid(),
            },
            status=200 if state is WorkerState.READY else 503,
        )

    def _state(self) -> WorkerState:
        if self._draining:
            return WorkerState.DRAINING
        if self._ready_checker is not None and not self._ready_checker():
            return WorkerState.STARTING
        return WorkerState.READY
