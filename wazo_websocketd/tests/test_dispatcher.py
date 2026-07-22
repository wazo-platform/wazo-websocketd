# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import socket
from unittest.mock import AsyncMock, Mock, patch

import pytest
import websockets
from websockets.datastructures import Headers

from ..auth import MasterTenantProxy
from ..dispatcher import Dispatcher, _extract_token_id, parse_request, resolve_token
from ..exception import AuthenticationError, NoTokenError
from ..ipc import ControlMessage, Handoff, Heartbeat, Revoke, WorkerWebSocketServer
from ..registry import WorkerRegistry
from ..worker import Worker
from .helpers import control_pair, echo_handler, make_tcp_listener, run_async

_TOKEN = {'token': 'tok', 'metadata': {'uuid': 'u', 'tenant_uuid': 't'}}


@pytest.fixture(autouse=True)
def _master_tenant():
    MasterTenantProxy.proxy.value = 'tenant-master'
    yield
    MasterTenantProxy.proxy.value = ''


def _make_worker(control_sock, **kwargs):
    return Worker(
        control_sock,
        handler_factory=lambda token_dict: echo_handler,
        ws_server=WorkerWebSocketServer(),
        **kwargs,
    )


def test_parse_request_extracts_path_and_headers():
    request = (
        b'GET /?token=abc&version=2 HTTP/1.1\r\n'
        b'Host: h\r\n'
        b'X-Auth-Token: hdr-tok\r\n'
        b'\r\n'
    )

    path, headers = parse_request(request)

    assert path == '/?token=abc&version=2'
    assert headers.get('X-Auth-Token') == 'hdr-tok'


def test_parse_request_malformed_request_line_falls_back_to_root():
    path, _ = parse_request(b'GARBAGE\r\n\r\n')

    assert path == '/'


def test_extract_token_id_from_path():
    assert _extract_token_id(Headers(), '/?token=abcdef') == 'abcdef'


def test_extract_token_id_from_header():
    assert _extract_token_id(Headers([('X-Auth-Token', 'abcdef')]), '/') == 'abcdef'


def test_extract_token_id_header_is_case_insensitive():
    assert _extract_token_id(Headers([('x-auth-token', 'abcdef')]), '/') == 'abcdef'


def test_extract_token_id_path_takes_precedence_over_header():
    headers = Headers([('X-Auth-Token', 'from-header')])
    assert _extract_token_id(headers, '/?token=from-path') == 'from-path'


def test_extract_token_id_missing_raises():
    with pytest.raises(NoTokenError):
        _extract_token_id(Headers(), '/')


def test_resolve_token_from_header():
    authenticator = Mock()
    authenticator.get_token = AsyncMock(return_value=_TOKEN)

    token = run_async(
        resolve_token(authenticator, '/', Headers([('X-Auth-Token', 'tok')]))
    )

    assert token == _TOKEN
    authenticator.get_token.assert_awaited_once_with('tok')


def test_resolve_token_no_token_does_not_call_auth():
    authenticator = Mock()
    authenticator.get_token = AsyncMock()

    with pytest.raises(NoTokenError):
        run_async(resolve_token(authenticator, '/', Headers()))
    authenticator.get_token.assert_not_called()


def test_resolve_token_propagates_authentication_error():
    authenticator = Mock()
    authenticator.get_token = AsyncMock(side_effect=AuthenticationError('bad'))

    with pytest.raises(AuthenticationError):
        run_async(resolve_token(authenticator, '/?token=x', Headers()))


def test_resolve_token_requires_master_tenant():
    authenticator = Mock()
    authenticator.get_token = AsyncMock()

    with patch.object(MasterTenantProxy, 'has_master_tenant', return_value=False):
        with pytest.raises(AuthenticationError):
            run_async(resolve_token(authenticator, '/?token=x', Headers()))
    authenticator.get_token.assert_not_called()


def test_handoff_round_trip():
    request = b'GET /?token=abc HTTP/1.1\r\nHost: h\r\n\r\n'

    handoff = Handoff.unpack(Handoff(_TOKEN, request).pack())

    assert handoff == Handoff(_TOKEN, request)


def test_control_message_round_trip():
    assert ControlMessage.unmarshal(Heartbeat(42).marshal()) == Heartbeat(42)
    assert ControlMessage.unmarshal(Revoke('tok-123').marshal()) == Revoke('tok-123')


def test_valid_token_handed_off_and_served():
    authenticator = Mock()
    authenticator.get_token = AsyncMock(return_value=_TOKEN)

    assert run_async(_dispatch_scenario(authenticator, token='good')) == 'echo:hi'


def test_bad_token_rejected_with_close_4002():
    authenticator = Mock()
    authenticator.get_token = AsyncMock(side_effect=AuthenticationError('bad'))

    assert run_async(_dispatch_scenario(authenticator, token='bad')) == 4002


def test_no_worker_available_rejected_with_close_1013():
    authenticator = Mock()
    authenticator.get_token = AsyncMock(return_value=_TOKEN)

    result = run_async(
        _dispatch_scenario(authenticator, token='good', start_worker=False)
    )

    assert result == 1013


def test_handoff_to_dead_worker_rejected_with_close_1013():
    authenticator = Mock()
    authenticator.get_token = AsyncMock(return_value=_TOKEN)

    assert run_async(_dead_worker_scenario(authenticator)) == 1013


async def _dead_worker_scenario(authenticator):
    loop = asyncio.get_event_loop()
    listener, port = make_tcp_listener()
    ctl_dispatcher, ctl_worker = control_pair()
    registry = WorkerRegistry()
    registry.add('w1', ctl_dispatcher)
    ctl_dispatcher.close()  # worker's control channel is gone
    ctl_worker.close()

    dispatcher = Dispatcher(loop, listener, authenticator, registry)
    run_task = asyncio.create_task(dispatcher.run())

    url = f'ws://127.0.0.1:{port}/?token=good'
    try:
        try:
            ws = await websockets.connect(url)
        except websockets.ConnectionClosed as e:
            return e.code
        try:
            await ws.send('hi')
            return await ws.recv()
        except websockets.ConnectionClosed as e:
            return e.code
        finally:
            await ws.close()
    finally:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        listener.close()


def test_failed_handoff_fails_over_to_a_healthy_worker():
    authenticator = Mock()
    authenticator.get_token = AsyncMock(return_value=_TOKEN)

    served, dead_draining = run_async(_failover_scenario(authenticator))

    assert served == 'echo:hi'
    assert dead_draining is True  # the unreachable worker is taken out of rotation


async def _failover_scenario(authenticator):
    loop = asyncio.get_event_loop()
    listener, port = make_tcp_listener()
    dead_dispatcher, dead_worker = control_pair()
    live_dispatcher, live_worker = control_pair()
    registry = WorkerRegistry()

    # The dead worker has the lowest count, so select() picks it first, but its
    # control channel is gone; the handoff must fail over to the live worker.
    registry.add('dead', dead_dispatcher)
    dead_dispatcher.close()
    dead_worker.close()
    registry.add('live', live_dispatcher)
    registry.heartbeat('live', connection_count=1)

    tasks = [
        asyncio.create_task(_make_worker(live_worker)._run()),
        asyncio.create_task(Dispatcher(loop, listener, authenticator, registry).run()),
    ]

    url = f'ws://127.0.0.1:{port}/?token=good'
    try:
        ws = await websockets.connect(url)
        try:
            await ws.send('hi')
            served = await ws.recv()
        finally:
            await ws.close()
        dead = registry.get('dead')
        return served, dead is not None and dead.draining
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        listener.close()
        live_dispatcher.close()
        live_worker.close()


def test_oversized_handoff_rejected_with_close_1011():
    huge_token = {
        'token': 'tok',
        'metadata': {'uuid': 'u', 'tenant_uuid': 't'},
        'acl': ['x' * 1024] * 200,  # ~200 KB payload, exceeds MAX_HANDOFF_SIZE
    }
    authenticator = Mock()
    authenticator.get_token = AsyncMock(return_value=huge_token)

    assert run_async(_dispatch_scenario(authenticator, token='good')) == 1011


def test_reject_closes_socket_when_adoption_fails():
    async def scenario():
        loop = asyncio.get_event_loop()
        sock, peer = socket.socketpair()
        try:
            with patch(
                'wazo_websocketd.dispatcher.adopt_connection',
                AsyncMock(side_effect=OSError('client reset')),
            ):
                dispatcher = Dispatcher(loop, Mock(), Mock(), Mock())
                await dispatcher._reject(sock, b'req', 1013, 'no worker available')
            return sock.fileno()  # -1 once the fd has been closed
        finally:
            peer.close()

    assert run_async(scenario()) == -1


def test_reject_aborts_transport_on_timeout():
    assert run_async(_reject_cleanup_scenario(cancel=False)) is True


def test_reject_aborts_transport_on_cancellation():
    assert run_async(_reject_cleanup_scenario(cancel=True)) is True


async def _reject_cleanup_scenario(cancel):
    loop = asyncio.get_event_loop()
    protocol = Mock()
    protocol.handler_task = loop.create_future()  # never completes on its own
    protocol.wait_closed = AsyncMock()
    protocol.transport = Mock()

    with patch(
        'wazo_websocketd.dispatcher.adopt_connection',
        AsyncMock(return_value=protocol),
    ):
        dispatcher = Dispatcher(loop, Mock(), Mock(), Mock())
        dispatcher._REJECT_TIMEOUT = 0.01
        task = loop.create_task(
            dispatcher._reject(Mock(), b'req', 1013, 'no worker available')
        )
        if cancel:
            await asyncio.sleep(0)  # let _reject reach wait_for
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return protocol.transport.abort.called


async def _dispatch_scenario(authenticator, token, start_worker=True):
    loop = asyncio.get_event_loop()
    listener, port = make_tcp_listener()
    ctl_dispatcher, ctl_worker = control_pair()
    registry = WorkerRegistry()

    tasks = []
    if start_worker:
        registry.add('w1', ctl_dispatcher)
        tasks.append(asyncio.create_task(_make_worker(ctl_worker)._run()))

    dispatcher = Dispatcher(loop, listener, authenticator, registry)
    tasks.append(asyncio.create_task(dispatcher.run()))

    url = f'ws://127.0.0.1:{port}/?token={token}'
    try:
        try:
            ws = await websockets.connect(url)
        except websockets.ConnectionClosed as e:
            return e.code
        try:
            await ws.send('hi')
            return await ws.recv()
        except websockets.ConnectionClosed as e:
            return e.code
        finally:
            await ws.close()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        listener.close()
        ctl_dispatcher.close()
        ctl_worker.close()


def test_run_survives_accept_error():
    loop = Mock()
    outcomes = [OSError(24, 'EMFILE'), asyncio.CancelledError()]

    async def sock_accept(_listener):
        raise outcomes.pop(0)

    loop.sock_accept = sock_accept
    dispatcher = Dispatcher(loop, Mock(), Mock(), Mock())

    with patch('wazo_websocketd.dispatcher.asyncio.sleep', AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            run_async(dispatcher.run())


def test_worker_control_socket_is_non_blocking():
    worker_sock, other_end = control_pair()
    try:
        _make_worker(worker_sock)
        assert worker_sock.getblocking() is False
    finally:
        worker_sock.close()
        other_end.close()


def test_worker_sends_heartbeat_with_current_count():
    worker_sock, reader_end = control_pair()
    worker = _make_worker(worker_sock)
    worker._connections = {Mock(), Mock(), Mock(), Mock()}

    worker.send_heartbeat()

    try:
        assert ControlMessage.unmarshal(reader_end.recv(4096)) == Heartbeat(4)
    finally:
        worker_sock.close()
        reader_end.close()


def test_stop_terminates_run_and_drains():
    done, serving = run_async(_termination_scenario())

    assert done is True
    assert serving is False


async def _termination_scenario():
    loop = asyncio.get_event_loop()
    worker_sock, other_end = control_pair()
    worker = _make_worker(worker_sock, heartbeat_interval=0.01)
    run_task = loop.create_task(worker._run())
    await asyncio.sleep(0.02)  # let run() reach the accept loop

    worker.stop()
    try:
        await asyncio.wait_for(run_task, timeout=2)
    finally:
        worker_sock.close()
        other_end.close()
    return run_task.done(), worker._ws_server.is_serving()


def test_drain_closes_live_connections_with_going_away():
    assert run_async(_drain_live_connection_scenario()) == 1001


async def _drain_live_connection_scenario():
    loop = asyncio.get_event_loop()
    listener, port = make_tcp_listener()
    ctl_dispatcher, ctl_worker = control_pair()
    registry = WorkerRegistry()
    registry.add('w1', ctl_dispatcher)

    async def idle_handler(ws, path):
        await ws.wait_closed()

    worker = Worker(
        ctl_worker,
        handler_factory=lambda token_dict: idle_handler,
        ws_server=WorkerWebSocketServer(),
        heartbeat_interval=10,
        drain_timeout=5,
    )
    authenticator = Mock()
    authenticator.get_token = AsyncMock(return_value=_TOKEN)
    tasks = [
        asyncio.create_task(worker._run()),
        asyncio.create_task(Dispatcher(loop, listener, authenticator, registry).run()),
    ]

    ws = await websockets.connect(f'ws://127.0.0.1:{port}/?token=good')
    try:
        for _ in range(200):
            if len(worker._connections) == 1:
                break
            await asyncio.sleep(0.01)
        worker.stop()
        await asyncio.wait_for(ws.wait_closed(), timeout=2)
        return ws.close_code
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        listener.close()
        ctl_dispatcher.close()
        ctl_worker.close()


def test_incomplete_request_is_closed_without_auth():
    assert run_async(_incomplete_request_scenario()) is False


async def _incomplete_request_scenario():
    loop = asyncio.get_event_loop()
    listener, _ = make_tcp_listener()
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client.close()  # peer connects then leaves without sending a request
    authenticator = Mock()
    authenticator.get_token = AsyncMock()
    dispatcher = Dispatcher(loop, listener, authenticator, WorkerRegistry())
    try:
        await dispatcher._handle_connection(server)
    finally:
        listener.close()
        server.close()
    return authenticator.get_token.called


def test_non_upgrade_request_is_closed_without_auth():
    request = b'GET /?token=good HTTP/1.1\r\nHost: h\r\n\r\n'
    assert run_async(_non_upgrade_scenario(request)) is False


def test_upgrade_request_reaches_authentication():
    request = (
        b'GET /?token=good HTTP/1.1\r\n'
        b'Host: h\r\n'
        b'Upgrade: websocket\r\n'
        b'Connection: Upgrade\r\n'
        b'\r\n'
    )
    assert run_async(_non_upgrade_scenario(request)) is True


async def _non_upgrade_scenario(request):
    loop = asyncio.get_event_loop()
    listener, _ = make_tcp_listener()
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client.sendall(request)
    client.close()
    authenticator = Mock()
    authenticator.get_token = AsyncMock(return_value=_TOKEN)
    dispatcher = Dispatcher(loop, listener, authenticator, WorkerRegistry())
    try:
        await dispatcher._handle_connection(server)
    finally:
        listener.close()
        server.close()
    return authenticator.get_token.called


def test_buffered_handoff_is_not_dropped_when_worker_stops():
    assert run_async(_stop_with_buffered_handoff()) == 1


async def _stop_with_buffered_handoff():
    loop = asyncio.get_event_loop()
    dispatcher_sock, worker_sock = control_pair()
    conn_a, conn_b = socket.socketpair()
    worker = _make_worker(worker_sock, heartbeat_interval=10)

    adopted = []

    async def fake_adopt(sock, payload):
        adopted.append(payload)
        sock.close()

    worker._adopt = fake_adopt

    payload = Handoff(_TOKEN, b'GET / HTTP/1.1\r\n\r\n').pack()
    socket.send_fds(dispatcher_sock, [payload], [conn_a.fileno()])
    worker.stop()  # already stopping before the accept loop runs

    run_task = loop.create_task(worker._run())
    try:
        await asyncio.wait_for(run_task, timeout=2)
    finally:
        for s in (dispatcher_sock, worker_sock, conn_a, conn_b):
            s.close()
    return len(adopted)


def test_worker_survives_malformed_payload():
    assert run_async(_malformed_payload_scenario()) is True


async def _malformed_payload_scenario():
    loop = asyncio.get_event_loop()
    dispatcher_sock, worker_sock = control_pair()
    conn_a, conn_b = socket.socketpair()
    worker = _make_worker(worker_sock, heartbeat_interval=10)
    run_task = loop.create_task(worker._run())
    await asyncio.sleep(0.02)
    try:
        socket.send_fds(dispatcher_sock, [b'not-json'], [conn_a.fileno()])
        await asyncio.sleep(0.05)
        still_running = not run_task.done()
        worker.stop()
        await asyncio.wait_for(run_task, timeout=2)
        return still_running
    finally:
        for s in (dispatcher_sock, worker_sock, conn_a, conn_b):
            s.close()
