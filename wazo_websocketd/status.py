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
from enum import StrEnum

from aiohttp import web

logger = logging.getLogger(__name__)

RUN_DIR = '/run/wazo-websocketd'
BROKER_ID = 'broker'
FETCH_TIMEOUT = 2.0
UNKNOWN_PID = 0
_UCRED = struct.Struct('3i')  # pid, uid, gid


class WorkerState(StrEnum):
    STARTING = 'starting'
    READY = 'ready'
    DRAINING = 'draining'


async def _probe_peer_pid(socket_path: str) -> int | None:
    """Pid serving `socket_path`, UNKNOWN_PID if unnameable, None if unserved."""
    probe = socket.socket(socket.AF_UNIX)
    probe.setblocking(False)
    with probe:
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().sock_connect(probe, socket_path),
                FETCH_TIMEOUT,
            )
            packed = probe.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size
            )
        except (ConnectionRefusedError, FileNotFoundError):
            return None
        except (OSError, TimeoutError):
            return UNKNOWN_PID

    pid, _, _ = _UCRED.unpack(packed)
    return pid


async def socket_peer_pid(socket_path: str) -> int | None:
    """Pid of the process listening on `socket_path`, or None if unknown."""
    return await _probe_peer_pid(socket_path) or None


async def serve_unix(runner: web.AppRunner, socket_path: str) -> None:
    await remove_stale_socket(socket_path)
    await web.UnixSite(runner, socket_path).start()
    os.chmod(socket_path, 0o600)


async def remove_stale_socket(socket_path: str) -> None:
    if not os.path.exists(socket_path):
        return
    if (owner := await _probe_peer_pid(socket_path)) is not None:
        served_by = f'pid {owner}' if owner else 'an unidentified process'
        raise OSError(f'{socket_path} is already served by {served_by}')
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
        listen: str,
        port: int,
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
