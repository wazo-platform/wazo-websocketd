# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import socket

import pytest
import websockets

from ..exception import ControlChannelClosed, HandoffError
from ..ipc import (
    WorkerWebSocketServer,
    adopt_connection,
    drain_pending_handoffs,
    read_handshake_request,
    receive_connection,
    send_connection,
)
from .helpers import control_pair, echo_handler, make_tcp_listener, run_async


def test_read_handshake_request_reads_up_to_terminator():
    request = b'GET /?token=x HTTP/1.1\r\nHost: h\r\nUpgrade: websocket\r\n\r\n'

    async def scenario():
        loop = asyncio.get_event_loop()
        reader, writer = socket.socketpair()
        try:
            writer.sendall(request)
            return await read_handshake_request(loop, reader)
        finally:
            reader.close()
            writer.close()

    assert run_async(scenario()) == request


def test_send_and_receive_passes_fd_and_payload():
    async def scenario():
        loop = asyncio.get_event_loop()
        ctl_send, ctl_recv = socket.socketpair()
        conn_local, conn_peer = socket.socketpair()
        try:
            send_connection(ctl_send, conn_local, b'payload-123')
            received, payload = await receive_connection(loop, ctl_recv)
            # Prove `received` is the same kernel connection as conn_local:
            # bytes written on its peer arrive on the handed-off socket.
            conn_peer.sendall(b'ping')
            received.setblocking(False)
            data = await loop.sock_recv(received, 4)
            received.close()
            return payload, data
        finally:
            ctl_send.close()
            ctl_recv.close()
            conn_local.close()
            conn_peer.close()

    payload, data = run_async(scenario())
    assert payload == b'payload-123'
    assert data == b'ping'


def test_receive_connection_handles_payload_larger_than_64k():
    async def scenario():
        loop = asyncio.get_event_loop()
        ctl_send, ctl_recv = control_pair()
        conn_local, conn_peer = socket.socketpair()
        try:
            send_connection(ctl_send, conn_local, b'x' * 90000)
            received, payload = await receive_connection(loop, ctl_recv)
            received.close()
            return len(payload)
        finally:
            for s in (ctl_send, ctl_recv, conn_local, conn_peer):
                s.close()

    assert run_async(scenario()) == 90000


def test_receive_connection_raises_on_truncated_payload():
    async def scenario():
        loop = asyncio.get_event_loop()
        ctl_send, ctl_recv = control_pair()
        conn_local, conn_peer = socket.socketpair()
        try:
            send_connection(ctl_send, conn_local, b'x' * 5000)
            await receive_connection(loop, ctl_recv, bufsize=1024)
        finally:
            for s in (ctl_send, ctl_recv, conn_local, conn_peer):
                s.close()

    with pytest.raises(HandoffError):
        run_async(scenario())


def test_receive_without_fd_raises():
    async def scenario():
        loop = asyncio.get_event_loop()
        ctl_send, ctl_recv = socket.socketpair()
        try:
            ctl_send.sendall(b'no fd here')
            await receive_connection(loop, ctl_recv)
        finally:
            ctl_send.close()
            ctl_recv.close()

    with pytest.raises(HandoffError):
        run_async(scenario())


def test_adopt_connection_completes_handshake_and_runs_handler():
    assert run_async(_roundtrip(message='hello')) == 'echo:hello'


def test_draining_worker_rejects_handshake_with_503():
    with pytest.raises(websockets.exceptions.InvalidStatusCode) as exc_info:
        run_async(_roundtrip(drain=True))
    assert exc_info.value.status_code == 503


async def _roundtrip(message='hello', drain=False):
    loop = asyncio.get_event_loop()
    listener, port = make_tcp_listener()

    async def server():
        conn, _ = await loop.sock_accept(listener)
        request = await read_handshake_request(loop, conn)
        ws_server = WorkerWebSocketServer()
        if drain:
            ws_server.start_draining()
        protocol = await adopt_connection(loop, conn, request, echo_handler, ws_server)
        await protocol.handler_task

    server_task = asyncio.create_task(server())
    try:
        async with websockets.connect(f'ws://127.0.0.1:{port}/') as ws:
            await ws.send(message)
            return await ws.recv()
    finally:
        await server_task
        listener.close()


def test_drain_pending_handoffs_returns_queued_connections():
    ctl_send, ctl_recv = control_pair()
    ctl_recv.setblocking(False)
    conn_a, peer_a = socket.socketpair()
    conn_b, peer_b = socket.socketpair()
    try:
        send_connection(ctl_send, conn_a, b'payload-a')
        send_connection(ctl_send, conn_b, b'payload-b')

        drained = drain_pending_handoffs(ctl_recv)

        payloads = [payload for _sock, payload in drained]
        assert payloads == [b'payload-a', b'payload-b']
        for sock, _payload in drained:
            sock.close()
    finally:
        for s in (ctl_send, ctl_recv, conn_a, peer_a, conn_b, peer_b):
            s.close()


def test_drain_pending_handoffs_returns_empty_when_nothing_queued():
    _ctl_send, ctl_recv = control_pair()
    ctl_recv.setblocking(False)
    try:
        assert drain_pending_handoffs(ctl_recv) == []
    finally:
        _ctl_send.close()
        ctl_recv.close()


def test_receive_on_closed_channel_raises_control_channel_closed():
    async def scenario():
        loop = asyncio.get_event_loop()
        sender, receiver = socket.socketpair()
        sender.close()  # the other end (dispatcher) went away
        try:
            await receive_connection(loop, receiver)
        finally:
            receiver.close()

    with pytest.raises(ControlChannelClosed):
        run_async(scenario())


def test_read_handshake_request_times_out(monkeypatch):
    monkeypatch.setattr('wazo_websocketd.ipc.HANDSHAKE_TIMEOUT', 0.05)

    async def scenario():
        loop = asyncio.get_event_loop()
        reader, writer = socket.socketpair()
        try:
            writer.sendall(b'GET / HTTP/1.1\r\n')  # no terminator, never finishes
            await read_handshake_request(loop, reader)
        finally:
            reader.close()
            writer.close()

    with pytest.raises(HandoffError):
        run_async(scenario())


def test_read_handshake_request_rejects_oversized(monkeypatch):
    monkeypatch.setattr('wazo_websocketd.ipc.MAX_REQUEST_SIZE', 16)

    async def scenario():
        loop = asyncio.get_event_loop()
        reader, writer = socket.socketpair()
        try:
            writer.sendall(b'GET /' + b'a' * 100)  # exceeds cap, no terminator
            await read_handshake_request(loop, reader)
        finally:
            reader.close()
            writer.close()

    with pytest.raises(HandoffError):
        run_async(scenario())
