# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
import requests
from aiohttp import web
from wazo_auth_client import Client as AuthClient

from ..auth import FallbackAuthClient
from ..broker import BrokerServer, SessionInvalidator, make_app
from ..exception import (
    AuthenticationError,
    AuthServerUnavailableError,
    BusConnectionError,
)
from ..token_cache import CachingAuthenticator
from .helpers import fake_wazo_auth

_BUS_CONFIG = {'bus': {'exchange_name': 'wazo-headers', 'exchange_type': 'headers'}}

_TOKEN = {
    'token': 'tok',
    'metadata': {'uuid': 'u', 'tenant_uuid': 't'},
    'utc_expires_at': '2999-01-01T00:00:00',
}


def _upstream(**get_token_kwargs):
    upstream = Mock()
    upstream.get_token = AsyncMock(**get_token_kwargs)
    return upstream


def _cache(upstream):
    return CachingAuthenticator(
        upstream,
        positive_ttl=10.0,
        negative_ttl=5.0,
        max_size=16 * 1024 * 1024,
        max_negative_entries=10000,
    )


@asynccontextmanager
async def _broker(cache):
    runner = web.AppRunner(make_app(cache))
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = runner.addresses[0][1]
    client = AuthClient('127.0.0.1', port=port, prefix=None, https=False)
    loop = asyncio.get_running_loop()

    def call(fn, *args, **kwargs):
        return loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    try:
        yield client, call
    finally:
        await runner.cleanup()


def _server_config(upstream, listen):
    return {
        'auth': upstream.config(),
        'bus': {
            'username': 'guest',
            'password': 'guest',
            'host': '127.0.0.1',
            'port': 1,
            'exchange_name': 'wazo-headers',
            'exchange_type': 'headers',
        },
        'broker': {'listen': listen, 'port': 0},
        'status': {'listen': '127.0.0.1', 'port': 0},
        'token_cache': {
            'positive_ttl': 10.0,
            'negative_ttl': 5.0,
            'max_size': 16 * 1024 * 1024,
            'max_negative_entries': 10000,
        },
    }


@asynccontextmanager
async def _started_server(config):
    server = BrokerServer(config)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def _get_over_unix_socket(socket_path, path):
    connector = aiohttp.UnixConnector(path=socket_path)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(f'http://localhost{path}') as response:
            return response.status, await response.json()


def _session_token(session_uuid):
    return {
        'token': 'tok',
        'session_uuid': session_uuid,
        'metadata': {'uuid': 'u', 'tenant_uuid': 't'},
        'utc_expires_at': '2999-01-01T00:00:00',
    }


def _marshaled(event):
    return json.dumps(event).encode()


class TestBrokerServer:
    async def test_server_starts_on_an_ipv6_listen_address(self):
        async with fake_wazo_auth() as upstream:
            config = _server_config(upstream, '::1')
            async with _started_server(config) as server:
                result = server.port
        assert isinstance(result, int)

    async def test_rejection_body_does_not_carry_the_token(self, tmp_path):
        secret = 'ac6b1c8e-0000-4000-8000-secrettokenid'
        socket_path = str(tmp_path / 'broker.sock')
        async with fake_wazo_auth() as upstream:
            upstream.statuses[secret] = 401
            config = _server_config(upstream, socket_path)
            async with _started_server(config):
                result = await _get_over_unix_socket(
                    socket_path, f'/0.1/token/{secret}'
                )
        status, body = result
        assert status == 401
        assert secret not in json.dumps(body)

    async def test_server_never_routes_its_upstream_through_itself(self, tmp_path):
        async with fake_wazo_auth() as upstream:
            config = _server_config(upstream, str(tmp_path / 'broker.sock'))
            server = BrokerServer(config)
            try:
                client = server._upstream_client
                result = isinstance(client, FallbackAuthClient), client._socket_path
            finally:
                await server._upstream_client.close()
        falls_back, socket_path = result
        assert falls_back is False  # reading through itself would be a loop
        assert socket_path is None  # straight to wazo-auth over TCP

    async def test_server_serves_validation_over_the_configured_unix_socket(
        self, tmp_path
    ):
        socket_path = str(tmp_path / 'broker.sock')
        async with fake_wazo_auth() as upstream:
            upstream.tokens['T'] = _TOKEN
            config = _server_config(upstream, socket_path)
            async with _started_server(config):
                result = await _get_over_unix_socket(
                    socket_path, '/0.1/token/T?scope=websocketd'
                )
        status, body = result
        assert status == 200
        assert body['data'] == _TOKEN

    async def test_status_shares_the_validation_unix_socket(self, tmp_path):
        socket_path = str(tmp_path / 'broker.sock')
        async with fake_wazo_auth() as upstream:
            config = _server_config(upstream, socket_path)
            async with _started_server(config):
                result = await _get_over_unix_socket(socket_path, '/status')
        status, body = result
        assert status == 200
        assert body['name'] == 'broker'

    async def test_server_removes_its_socket_on_stop(self, tmp_path):
        socket_path = tmp_path / 'broker.sock'
        async with fake_wazo_auth() as upstream:
            config = _server_config(upstream, str(socket_path))
            async with _started_server(config):
                pass
            result = socket_path.exists()
        assert result is False

    async def test_server_serves_the_configured_address_from_config(self):
        async with fake_wazo_auth() as upstream:
            upstream.tokens['T'] = _TOKEN
            config = _server_config(upstream, '127.0.0.1')
            async with _started_server(config) as server:
                client = AuthClient(
                    '127.0.0.1', port=server.port, prefix=None, https=False
                )
                loop = asyncio.get_running_loop()
                first = await loop.run_in_executor(
                    None, client.token.get, 'T', 'websocketd'
                )
                second = await loop.run_in_executor(
                    None, client.token.get, 'T', 'websocketd'
                )
            result = first, second, upstream.requests
        first, second, upstream_seen = result
        assert first == second == _TOKEN
        assert upstream_seen == [('GET', 'T', 'websocketd')]


class TestTokenValidationApi:
    async def test_get_token_speaks_the_wazo_auth_api(self):
        upstream = _upstream(return_value=_TOKEN)
        cache = _cache(upstream)
        async with _broker(cache) as (client, call):
            result = await call(client.token.get, 'T', 'chatd.users.read')
        assert result == _TOKEN
        upstream.get_token.assert_awaited_once_with('T', 'chatd.users.read')

    async def test_get_token_is_cached_per_token_and_acl(self):
        upstream = _upstream(return_value=_TOKEN)
        cache = _cache(upstream)
        async with _broker(cache) as (client, call):
            await call(client.token.get, 'T', 'websocketd')
            await call(client.token.get, 'T', 'websocketd')
            await call(client.token.get, 'T', 'chatd.users.read')
        assert upstream.get_token.await_count == 2

    async def test_get_token_without_scope_shares_the_default_scope_entry(self):
        upstream = _upstream(return_value=_TOKEN)
        cache = _cache(upstream)
        async with _broker(cache) as (client, call):
            await call(client.token.get, 'T', 'websocketd')
            await call(client.token.get, 'T')
        assert upstream.get_token.await_count == 1

    async def test_get_token_relays_upstream_rejection_status(self):
        upstream = _upstream(
            side_effect=AuthenticationError('unknown token', status_code=404)
        )
        cache = _cache(upstream)
        async with _broker(cache) as (client, call):
            with pytest.raises(requests.HTTPError) as excinfo:
                await call(client.token.get, 'T', 'websocketd')
            result = excinfo.value.response.status_code
        assert result == 404

    async def test_get_token_returns_503_when_auth_unavailable(self):
        upstream = _upstream(side_effect=AuthServerUnavailableError('down'))
        cache = _cache(upstream)
        async with _broker(cache) as (client, call):
            with pytest.raises(requests.HTTPError) as excinfo:
                await call(client.token.get, 'T', 'websocketd')
            result = excinfo.value.response.status_code
        assert result == 503

    async def test_head_valid_token_reports_valid(self):
        upstream = _upstream(return_value=_TOKEN)
        cache = _cache(upstream)
        async with _broker(cache) as (client, call):
            result = await call(client.token.is_valid, 'T', 'websocketd')
        assert result is True
        upstream.get_token.assert_awaited_once_with('T', 'websocketd')

    async def test_head_rejected_token_reports_invalid(self):
        upstream = _upstream(side_effect=AuthenticationError('denied', status_code=403))
        cache = _cache(upstream)
        async with _broker(cache) as (client, call):
            result = await call(client.token.is_valid, 'T', 'websocketd')
        assert result is False

    async def test_head_returns_503_when_auth_unavailable(self):
        upstream = _upstream(side_effect=AuthServerUnavailableError('down'))
        cache = _cache(upstream)
        async with _broker(cache) as (client, call):
            with pytest.raises(requests.HTTPError) as excinfo:
                await call(client.token.is_valid, 'T', 'websocketd')
            result = excinfo.value.response.status_code
        assert result == 503

    async def test_head_and_get_share_the_cache(self):
        upstream = _upstream(return_value=_TOKEN)
        cache = _cache(upstream)
        async with _broker(cache) as (client, call):
            await call(client.token.is_valid, 'T', 'websocketd')
            result = await call(client.token.get, 'T', 'websocketd')
        assert result == _TOKEN
        assert upstream.get_token.await_count == 1


class TestSessionInvalidator:
    async def test_session_deleted_event_refuses_the_session_tokens(self):
        upstream = _upstream(return_value=_session_token('session-1'))
        cache = _cache(upstream)
        invalidator = SessionInvalidator(Mock(), _BUS_CONFIG, cache)
        event = {'name': 'auth_session_deleted', 'data': {'uuid': 'session-1'}}
        await cache.get_token('T')
        await invalidator._on_event(None, _marshaled(event), None, None)
        with pytest.raises(AuthenticationError):
            await cache.get_token('T')  # revoked: refused without asking upstream
        assert upstream.get_token.await_count == 1

    async def test_unwrapped_session_deleted_payload_is_understood(self):
        upstream = _upstream(return_value=_session_token('session-2'))
        cache = _cache(upstream)
        invalidator = SessionInvalidator(Mock(), _BUS_CONFIG, cache)
        event = {'name': 'auth_session_deleted', 'uuid': 'session-2'}
        await cache.get_token('T')
        await invalidator._on_event(None, _marshaled(event), None, None)
        with pytest.raises(AuthenticationError):
            await cache.get_token('T')
        assert upstream.get_token.await_count == 1

    async def test_malformed_session_deleted_event_is_discarded(self):
        upstream = _upstream(return_value=_session_token('session-3'))
        cache = _cache(upstream)
        invalidator = SessionInvalidator(Mock(), _BUS_CONFIG, cache)
        await cache.get_token('T')
        await invalidator._on_event(None, b'not-json', None, None)
        await invalidator._on_event(None, _marshaled({'data': {}}), None, None)
        result = await cache.get_token('T')
        assert result == _session_token('session-3')
        assert upstream.get_token.await_count == 1

    async def test_invalidator_flushes_then_degrades_with_bus_state(self):
        upstream = _upstream(return_value=_session_token('session-4'))
        cache = _cache(upstream)

        channel = AsyncMock()
        channel.queue.return_value = {'queue': 'q'}

        connection = Mock()
        connection.is_closing = False
        channels = [channel]

        async def get_channel():
            if channels:
                return channels.pop()
            raise BusConnectionError('closing')

        connection.get_channel = get_channel
        invalidator = SessionInvalidator(connection, _BUS_CONFIG, cache)
        cache.eviction_lost()
        await cache.get_token('T')  # degraded entry, then flushed on subscribe

        task = asyncio.create_task(invalidator.run())
        await asyncio.sleep(0.01)  # let it subscribe

        await cache.get_token('T')  # miss after the flush: refetch, full ttl

        await invalidator.connection_lost()
        connection.is_closing = True
        await asyncio.wait_for(task, timeout=1)

        assert upstream.get_token.await_count == 2
        channel.queue_bind.assert_awaited_once_with(
            'q',
            'wazo-headers',
            '',
            arguments={'x-match': 'all', 'name': 'auth_session_deleted'},
        )
