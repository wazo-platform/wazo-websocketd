# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from xivo.auth_verifier import AccessCheck

from ..config import _DEFAULT_CONFIG
from ..consumers import BusEvent, SessionInvalidator, UserBusSubscriber
from ..exception import (
    AuthenticationError,
    BusConnectionError,
    BusConnectionLostError,
    EventPermissionError,
    InvalidEvent,
)
from .helpers import BUS_CONFIG as _BUS_CONFIG
from .helpers import caching_authenticator, marshaled, session_token, token_provider


def _token(**metadata):
    now = f'{datetime.now()}'
    return {
        'token': f'{uuid4()}',
        'session_uuid': f'{uuid4()}',
        'acl': ['some.acl'],
        'metadata': {
            'uuid': f'{uuid4()}',
            'tenant_uuid': f'{uuid4()}',
            'auth_id': f'{uuid4()}',
            'pbx_user_uuid': f'{uuid4()}',
            'xivo_uuid': f'{uuid4()}',
            **metadata,
        },
        'utc_expires_at': now,
        'expires_at': now,
        'issued_at': now,
        'utc_issued_at': now,
        'user_agent': 'some-user-agent',
        'remote_addr': '127.0.0.1',
        'xivo_uuid': f'{uuid4()}',
        'auth_id': f'{uuid4()}',
    }


def _consumer(origin_uuid=None, **metadata):
    config = dict(_DEFAULT_CONFIG, uuid=origin_uuid or Mock())
    return UserBusSubscriber(Mock(), config, _token(**metadata))


def _properties(**headers):
    return Mock(headers=headers)


class TestEventDecoding:
    def setup_method(self):
        self.consumer = _consumer()

    def test_a_decoded_event_carries_its_name_acl_and_payload(self):
        properties = _properties(name='foo', required_acl='some.acl')

        event = self.consumer._decode_content(b'{}', properties)

        assert event.name == 'foo'
        assert event.acl == 'some.acl'
        assert event.content == {}
        assert event.raw == '{}'

    def test_an_event_without_a_required_acl_is_decoded_with_none(self):
        properties = _properties(name='foo', required_acl=None)

        event = self.consumer._decode_content(b'{}', properties)

        assert event == BusEvent('foo', properties.headers, None, {}, '{}')

    def test_an_event_missing_its_required_acl_header_is_refused(self):
        with pytest.raises(EventPermissionError):
            self.consumer._decode_content(b'{}', _properties(name='foo'))

    @pytest.mark.parametrize(
        ('content', 'headers'),
        [
            pytest.param(b'{}', {'required_acl': 'some.acl'}, id='missing name'),
            pytest.param(
                b'{}',
                {'name': None, 'required_acl': 'some.acl'},
                id='name is not a string',
            ),
            pytest.param(
                b'{}', {'name': 'foo', 'required_acl': 2}, id='acl is not a string'
            ),
            pytest.param(
                b'{"name": "\xe8"}',
                {'name': 'foo', 'required_acl': 'some.acl'},
                id='undecodable bytes',
            ),
            pytest.param(
                b'{invalid',
                {'name': 'foo', 'required_acl': 'some.acl'},
                id='invalid json',
            ),
            pytest.param(
                b'2',
                {'name': 'foo', 'required_acl': 'some.acl'},
                id='root is not an object',
            ),
        ],
    )
    def test_a_malformed_event_is_refused(self, content, headers):
        with pytest.raises(InvalidEvent):
            self.consumer._decode_content(content, _properties(**headers))


class TestEventDispatching:
    def setup_method(self):
        self.consumer = _consumer()
        self.consumer._access = Mock(AccessCheck)

    async def _deliver(self, content, **headers):
        channel = Mock(basic_client_ack=AsyncMock())
        await self.consumer._consumer._on_message(
            channel, content, Mock(delivery_tag=1), _properties(**headers)
        )
        channel.basic_client_ack.assert_awaited_once()

    async def test_a_lost_connection_is_raised_to_whoever_is_consuming(self):
        await self.consumer._consumer.connection_lost()

        with pytest.raises(BusConnectionLostError):
            async for _ in self.consumer:
                pass

    async def test_a_delivered_event_is_handed_to_whoever_is_consuming(self):
        await self._deliver(b'{"id": 7}', name='foo', required_acl='some.acl')

        async for event in self.consumer:
            assert event == BusEvent(
                'foo',
                {'name': 'foo', 'required_acl': 'some.acl'},
                'some.acl',
                {'id': 7},
                '{"id": 7}',
            )
            break

    async def test_an_event_the_user_may_not_see_is_never_handed_over(self):
        self.consumer._access = Mock(**{'matches_required_access.return_value': False})

        await self._deliver(b'{"id": 7}', name='foo', required_acl='other.acl')

        # nothing was queued, so consuming would block rather than yield it
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(self.consumer.__anext__(), timeout=0.05)


class TestEventBindings:
    def test_a_user_binds_to_its_own_events_and_to_the_broadcasts(self):
        user_uuid = str(uuid4())
        consumer = _consumer(purpose='user', admin=False, uuid=user_uuid)

        assert consumer._generate_bindings('some_event') == [
            {'name': 'some_event', f'user_uuid:{user_uuid}': True},
            {'name': 'some_event', 'user_uuid:*': True},
        ]
        assert consumer._generate_bindings('*') == [
            {f'user_uuid:{user_uuid}': True},
            {'user_uuid:*': True},
        ]

    @pytest.mark.parametrize(
        'metadata',
        [
            pytest.param({'purpose': 'user', 'admin': True}, id='tenant admin'),
            pytest.param({'purpose': 'internal'}, id='internal'),
            pytest.param({'purpose': 'external_api'}, id='external api'),
        ],
    )
    def test_a_privileged_token_binds_to_everything_from_this_origin(self, metadata):
        origin_uuid = Mock()
        consumer = _consumer(origin_uuid=origin_uuid, **metadata)

        assert consumer._generate_bindings('some_event') == [
            {'name': 'some_event', 'origin_uuid': origin_uuid},
        ]
        assert consumer._generate_bindings('*') == [{'origin_uuid': origin_uuid}]


def _bus_connection():
    channel = AsyncMock()
    channel.queue.return_value = {'queue': 'q'}
    connection = Mock(is_closing=False, get_channel=AsyncMock(return_value=channel))
    return connection, channel


@asynccontextmanager
async def _running_invalidator(cache):
    """Runs an invalidator, yielding a callable that delivers one bus event."""
    connection, channel = _bus_connection()
    invalidator = SessionInvalidator(connection, _BUS_CONFIG, cache)
    consumer = invalidator._consumer
    task = asyncio.create_task(invalidator.run())
    await asyncio.sleep(0)  # let it subscribe

    async def deliver(content):
        await consumer._on_message(channel, content, Mock(delivery_tag=1), Mock())
        await asyncio.sleep(0)  # let the run loop act on it

    try:
        yield deliver
    finally:
        connection.is_closing = True
        await consumer.connection_lost()
        await asyncio.wait_for(task, timeout=1)


class TestSessionInvalidator:
    async def test_session_deleted_event_refuses_the_session_tokens(self):
        upstream = token_provider(return_value=session_token('session-1'))
        cache = caching_authenticator(upstream)
        event = {'name': 'auth_session_deleted', 'data': {'uuid': 'session-1'}}

        async with _running_invalidator(cache) as deliver:
            await cache.get_token('T')
            await deliver(marshaled(event))

        with pytest.raises(AuthenticationError):
            await cache.get_token('T')  # revoked: refused without asking upstream
        assert upstream.get_token.await_count == 1

    async def test_unwrapped_session_deleted_payload_is_understood(self):
        upstream = token_provider(return_value=session_token('session-2'))
        cache = caching_authenticator(upstream)
        event = {'name': 'auth_session_deleted', 'uuid': 'session-2'}

        async with _running_invalidator(cache) as deliver:
            await cache.get_token('T')
            await deliver(marshaled(event))

        with pytest.raises(AuthenticationError):
            await cache.get_token('T')
        assert upstream.get_token.await_count == 1

    async def test_malformed_session_deleted_event_is_discarded(self):
        upstream = token_provider(return_value=session_token('session-3'))
        cache = caching_authenticator(upstream)

        async with _running_invalidator(cache) as deliver:
            await cache.get_token('T')
            await deliver(b'not-json')
            await deliver(marshaled({'data': {}}))

            # asserted before leaving: losing the bus flushes the cache
            result = await cache.get_token('T')

        assert result == session_token('session-3')
        assert upstream.get_token.await_count == 1

    async def test_invalidator_flushes_then_degrades_with_bus_state(self):
        upstream = token_provider(return_value=session_token('session-4'))
        cache = caching_authenticator(upstream)

        channel = AsyncMock()
        channel.queue.return_value = {'queue': 'q'}

        connection = Mock()
        connection.is_closing = False
        channels = [channel]

        async def get_channel(*, wait=False):
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

        await invalidator._consumer.connection_lost()
        connection.is_closing = True
        await asyncio.wait_for(task, timeout=1)

        assert upstream.get_token.await_count == 2
        channel.queue_bind.assert_awaited_once_with(
            'q',
            'wazo-headers',
            '',
            arguments={'name': 'auth_session_deleted'},
        )
