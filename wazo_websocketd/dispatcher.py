# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import logging
import socket
from urllib.parse import parse_qsl, urlparse

from websockets.datastructures import Headers

from .auth import MasterTenantProxy, TokenAuthenticator
from .exception import AuthenticationError, AuthServerUnavailableError, NoTokenError
from .ipc import (
    REQUEST_TERMINATOR,
    Handoff,
    WorkerWebSocketServer,
    adopt_connection,
    read_handshake_request,
    send_connection,
)
from .protocol import CloseCode
from .registry import WorkerRegistry

logger = logging.getLogger(__name__)


def parse_request(request_bytes: bytes) -> tuple[str, Headers]:
    head = request_bytes.split(b'\r\n\r\n', 1)[0].decode('latin-1')
    lines = head.split('\r\n')
    # "GET /path?query HTTP/1.1"
    request_line = lines[0].split(' ') if lines else []
    path = request_line[1] if len(request_line) >= 2 else '/'
    headers = Headers()
    for line in lines[1:]:
        name, sep, value = line.partition(':')
        if sep:
            headers[name.strip()] = value.strip()
    return path, headers


async def resolve_token(
    authenticator: TokenAuthenticator, path: str, request_headers: Headers
) -> dict:
    if not MasterTenantProxy.has_master_tenant():
        raise AuthenticationError('unable to determine master tenant')
    token_id = _extract_token_id(request_headers, path)
    return await authenticator.get_token(token_id)


def _extract_token_id(request_headers: Headers, path: str) -> str:
    for name, value in parse_qsl(urlparse(path).query):
        if name == 'token':
            return value
    if (token := request_headers.get('x-auth-token')) is not None:
        return token
    raise NoTokenError()


class Dispatcher:
    _ACCEPT_RETRY_DELAY = 0.1

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        listener: socket.socket,
        authenticator: TokenAuthenticator,
        registry: WorkerRegistry,
    ) -> None:
        self._loop = loop
        self._listener = listener
        self._authenticator = authenticator
        self._registry = registry
        self._connections: set[asyncio.Task] = set()
        # Used only to complete handshakes for rejected connections so the
        # client receives a WebSocket close code (see Client rejection contract).
        self._reject_server = WorkerWebSocketServer()

    async def run(self) -> None:
        while True:
            try:
                sock, _ = await self._loop.sock_accept(self._listener)
            except OSError as exc:
                logger.warning('failed to accept a connection: %s', exc)
                await asyncio.sleep(self._ACCEPT_RETRY_DELAY)
                continue
            task = self._loop.create_task(self._handle_connection(sock))
            self._connections.add(task)
            task.add_done_callback(self._connections.discard)

    async def _handle_connection(self, sock: socket.socket) -> None:
        try:
            request = await read_handshake_request(self._loop, sock)
            if REQUEST_TERMINATOR not in request:
                logger.debug('closing connection with no complete HTTP request')
                sock.close()
                return
            path, headers = parse_request(request)
            try:
                token = await resolve_token(self._authenticator, path, headers)
            except NoTokenError:
                await self._reject(sock, request, CloseCode.NO_TOKEN, 'no token')
                return
            except AuthenticationError:
                await self._reject(
                    sock, request, CloseCode.AUTH_FAILED, 'authentication failed'
                )
                return
            except AuthServerUnavailableError:
                await self._reject(
                    sock,
                    request,
                    CloseCode.TRY_LATER,
                    'authentication temporarily unavailable',
                )
                return

            entry = self._registry.select()
            if entry is None:
                await self._reject(
                    sock, request, CloseCode.TRY_LATER, 'no worker available'
                )
                return

            send_connection(entry.control_sock, sock, Handoff(token, request).pack())
            self._registry.note_handoff(entry)
            sock.close()  # close our copy after the fd is transferred
        except Exception:
            logger.exception('error while handling connection')
            sock.close()

    async def _reject(
        self, sock: socket.socket, request: bytes, code: int, reason: str
    ) -> None:
        async def reject_handler(ws, path):
            await ws.close(code, reason)

        protocol = await adopt_connection(
            self._loop, sock, request, reject_handler, self._reject_server
        )
        await protocol.handler_task
        await protocol.wait_closed()
