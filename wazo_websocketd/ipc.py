# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import socket
from base64 import b64decode, b64encode
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import ClassVar

from websockets.legacy.server import WebSocketServer, WebSocketServerProtocol

from .exception import ControlChannelClosed, HandoffError

logger = logging.getLogger(__name__)

REQUEST_TERMINATOR = b'\r\n\r\n'
HANDSHAKE_TIMEOUT = 10.0
MAX_REQUEST_SIZE = 65536
MAX_HANDOFF_SIZE = MAX_REQUEST_SIZE * 2

WebSocketHandler = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class Handoff:
    token: dict
    request: bytes

    def pack(self) -> bytes:
        return json.dumps(
            {'token': self.token, 'request': b64encode(self.request).decode('ascii')}
        ).encode('utf-8')

    @staticmethod
    def unpack(raw: bytes) -> Handoff:
        obj = json.loads(raw)
        return Handoff(obj['token'], b64decode(obj['request']))


@dataclass(frozen=True)
class ControlMessage:
    _op: ClassVar[str]
    _registry: ClassVar[dict[str, type[ControlMessage]]] = {}

    def __init_subclass__(cls, *, op: str, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if not op:
            raise ValueError(f'{cls.__name__} must declare a non-empty op')

        if op in ControlMessage._registry:
            raise ValueError(
                f'op {op!r} already registered by '
                f'{ControlMessage._registry[op].__name__}'
            )

        cls._op = op
        ControlMessage._registry[op] = cls

    def marshal(self) -> bytes:
        return json.dumps({'op': self._op, **asdict(self)}).encode('utf-8')

    @staticmethod
    def unmarshal(raw: bytes) -> ControlMessage:
        payload = json.loads(raw)
        return ControlMessage._registry[payload.pop('op')](**payload)


@dataclass(frozen=True)
class Heartbeat(ControlMessage, op='heartbeat'):
    count: int


@dataclass(frozen=True)
class Revoke(ControlMessage, op='revoke'):
    token_id: str


class WorkerWebSocketServer(WebSocketServer):
    # A handoff worker has no listening socket, but the handshake calls
    # ws_server.is_serving() which the base delegates to a (missing) asyncio
    # server. Override it with a drain flag: draining -> new handshakes get 503.
    def __init__(self, logger: logging.Logger | None = None) -> None:
        super().__init__(logger=logger)
        self._serving = True

    def is_serving(self) -> bool:
        return self._serving

    def start_draining(self) -> None:
        self._serving = False


async def read_handshake_request(
    loop: asyncio.AbstractEventLoop, sock: socket.socket
) -> bytes:
    sock.setblocking(False)
    try:
        return await asyncio.wait_for(
            _read_until_terminator(loop, sock), HANDSHAKE_TIMEOUT
        )
    except TimeoutError as e:
        raise HandoffError('timed out reading handshake request') from e


async def _read_until_terminator(
    loop: asyncio.AbstractEventLoop, sock: socket.socket
) -> bytes:
    buffer = b''
    while REQUEST_TERMINATOR not in buffer:  # a handshake request has no body
        chunk = await loop.sock_recv(sock, 4096)
        if not chunk:
            break

        buffer += chunk
        if len(buffer) > MAX_REQUEST_SIZE:
            raise HandoffError('handshake request exceeds maximum size')
    return buffer


def send_connection(
    control_sock: socket.socket, sock: socket.socket, payload: bytes
) -> None:
    # SCM_RIGHTS dups the fd; the caller must close its own copy of `sock` only
    # after this returns.
    socket.send_fds(control_sock, [payload], [sock.fileno()])


async def receive_connection(
    loop: asyncio.AbstractEventLoop,
    control_sock: socket.socket,
    bufsize: int = MAX_HANDOFF_SIZE,
) -> tuple[socket.socket, bytes]:
    await _wait_readable(loop, control_sock)
    payload, fds, msg_flags, _addr = socket.recv_fds(control_sock, bufsize, 1)
    if msg_flags & socket.MSG_TRUNC:
        for fd in fds:
            os.close(fd)
        raise HandoffError('handoff payload exceeded the receive buffer')
    if not payload and not fds:
        raise ControlChannelClosed()
    if not fds:
        raise HandoffError('handoff message carried no file descriptor')
    return socket.socket(fileno=fds[0]), payload


def drain_pending_handoffs(
    control_sock: socket.socket, bufsize: int = MAX_HANDOFF_SIZE
) -> list[tuple[socket.socket, bytes]]:
    pending: list[tuple[socket.socket, bytes]] = []

    while True:
        try:
            payload, fds, msg_flags, _addr = socket.recv_fds(control_sock, bufsize, 1)
        except OSError:
            return pending

        if not payload and not fds:
            return pending

        if msg_flags & socket.MSG_TRUNC:
            for fd in fds:
                os.close(fd)
            continue

        if fds:
            pending.append((socket.socket(fileno=fds[0]), payload))


async def _wait_readable(loop: asyncio.AbstractEventLoop, sock: socket.socket) -> None:
    future: asyncio.Future[None] = loop.create_future()
    loop.add_reader(sock.fileno(), future.set_result, None)
    try:
        await future
    finally:
        loop.remove_reader(sock.fileno())


async def adopt_connection(
    loop: asyncio.AbstractEventLoop,
    sock: socket.socket,
    request_bytes: bytes,
    ws_handler: WebSocketHandler,
    ws_server: WebSocketServer,
) -> WebSocketServerProtocol:
    sock.setblocking(False)
    factory = functools.partial(
        WebSocketServerProtocol,
        ws_handler,
        ws_server,
        logger=ws_server.logger,
    )
    _transport, protocol = await loop.connect_accepted_socket(factory, sock)
    # No race: the client sends nothing until it sees the 101, so replaying the
    # already-consumed request bytes here always precedes any frame data.
    protocol.data_received(request_bytes)
    return protocol
