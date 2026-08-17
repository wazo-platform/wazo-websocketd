# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from aioamqp.exceptions import ChannelClosed
from xivo.auth_verifier import AccessCheck

from ..bus import BusConnection, _BusConsumer
from ..config import _DEFAULT_CONFIG
from ..consumers import BusEvent, SessionInvalidator, UserBusSubscriber
from ..exception import (
    AuthenticationError,
    BusConnectionError,
    BusConnectionLostError,
    EventPermissionError,
    InvalidEvent,
    SessionRevokedError,
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
        self.consumer._subscriptions.add('foo')

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
        queue, exchange, routing_key = channel.queue_bind.await_args.args
        assert queue.startswith('wazo-websocketd.broker-')
        assert (exchange, routing_key) == ('wazo-headers', '')
        assert channel.queue_bind.await_args.kwargs == {
            'no_wait': True,
            'arguments': {'name': 'auth_session_deleted'},
        }


class _FakeChannel:
    def __init__(self, fail_bind_with=None):
        self._fail_bind_with = fail_bind_with
        self.bind_arguments = []
        self.is_open = True

    async def exchange(self, *args, **kwargs):
        pass

    async def exchange_bind(self, *args, **kwargs):
        pass

    async def basic_qos(self, *args, **kwargs):
        pass

    async def queue(self, name, **kwargs):
        return {'queue': name}

    async def basic_consume(self, *args, **kwargs):
        return {'consumer_tag': 'tag'}

    async def close(self):
        self.is_open = False

    async def queue_bind(
        self, queue, exchange, routing_key, no_wait=False, arguments=None
    ):
        if self._fail_bind_with is not None:
            raise self._fail_bind_with
        self.bind_arguments.append(arguments)


def _revocable_subscriber(**token_fields):
    token = _token(uuid='user-1')
    token['session_uuid'] = 'session-1'
    token.update(token_fields)
    return UserBusSubscriber(Mock(), dict(_DEFAULT_CONFIG, uuid='origin-uuid'), token)


def _subscriber_over(*channels):
    subscriber = _revocable_subscriber()
    opened = list(channels)
    subscriber._consumer._connection = Mock(
        get_channel=AsyncMock(side_effect=lambda **_: opened.pop(0))
    )
    return subscriber


async def _deliver_to(subscriber, name, body, acl='some.acl'):
    channel = Mock(basic_client_ack=AsyncMock())
    properties = Mock(headers={'name': name, 'required_acl': acl})
    await subscriber._consumer._on_message(
        channel, json.dumps(body).encode(), Mock(delivery_tag='tag'), properties
    )


class TestSessionRevocation:
    async def test_the_deletion_of_our_own_session_terminates_the_subscriber(self):
        subscriber = _revocable_subscriber()

        # enforcement must not depend on the client being allowed to see the event
        await _deliver_to(
            subscriber,
            'auth_session_deleted',
            {'data': {'uuid': 'session-1'}},
            acl='nope',
        )

        with pytest.raises(SessionRevokedError):
            await subscriber.__anext__()

    async def test_the_deletion_of_another_session_is_ignored(self):
        subscriber = _revocable_subscriber()

        await _deliver_to(
            subscriber, 'auth_session_deleted', {'data': {'uuid': 'other'}}
        )

        assert subscriber._consumer._queue.empty()

    async def test_renewing_the_token_follows_the_new_session(self):
        subscriber = _revocable_subscriber()
        renewed = _token(uuid='user-1')
        renewed['session_uuid'] = 'session-9'

        subscriber.set_token(renewed)
        await _deliver_to(
            subscriber, 'auth_session_deleted', {'data': {'uuid': 'session-9'}}
        )

        with pytest.raises(SessionRevokedError):
            await subscriber.__anext__()

    def test_a_token_for_another_user_is_refused(self):
        subscriber = _revocable_subscriber()

        with pytest.raises(AuthenticationError):
            subscriber.set_token(_token(uuid='user-2'))


class TestEventFiltering:
    async def test_an_event_nobody_subscribed_to_is_not_forwarded(self):
        subscriber = _revocable_subscriber()

        await _deliver_to(subscriber, 'some_event', {'data': {}})

        assert subscriber._consumer._queue.empty()

    @pytest.mark.parametrize(
        'subscription',
        [pytest.param('some_event', id='by name'), pytest.param('*', id='wildcard')],
    )
    async def test_a_subscribed_event_is_forwarded(self, subscription):
        subscriber = _revocable_subscriber()
        subscriber._subscriptions.add(subscription)

        await _deliver_to(subscriber, 'some_event', {'data': {}})

        assert (await subscriber.__anext__()).name == 'some_event'


class TestSubscriberSetup:
    async def test_a_tenant_exchange_that_vanished_mid_setup_is_redeclared(self):
        # the exchange is auto_delete, so the tenant's last session leaving
        # between our declare and our bind takes it with them
        gone = ChannelClosed(404, "NOT_FOUND - no exchange 'wazo-websocketd.tenant-x'")
        first, second = _FakeChannel(fail_bind_with=gone), _FakeChannel()
        subscriber = _subscriber_over(first, second)

        await subscriber._consumer._start_consuming()

        assert subscriber._consumer._channel is second
        assert second.bind_arguments == [
            {'name': 'auth_session_deleted', 'user_uuid:user-1': True}
        ]

    async def test_setup_gives_up_after_repeated_losses(self):
        gone = ChannelClosed(404, 'NOT_FOUND')
        attempts = _BusConsumer._SETUP_RETRIES + 1
        subscriber = _subscriber_over(
            *(_FakeChannel(fail_bind_with=gone) for _ in range(attempts))
        )

        with patch.object(_BusConsumer, '_SETUP_DELAY', 0):
            with pytest.raises(ChannelClosed):
                await subscriber._consumer._start_consuming()

    async def test_a_refused_binding_is_not_retried(self):
        refused = ChannelClosed(403, 'ACCESS_REFUSED')
        subscriber = _subscriber_over(
            _FakeChannel(fail_bind_with=refused), _FakeChannel()
        )

        with pytest.raises(ChannelClosed) as raised:
            await subscriber._consumer._start_consuming()

        assert raised.value.code == 403

    async def test_a_subscriber_whose_setup_failed_is_off_the_connection(self):
        connection = BusConnection('amqp://localhost')
        connection.get_channel = AsyncMock(  # type: ignore[method-assign]
            return_value=_FakeChannel(fail_bind_with=ChannelClosed(403, 'REFUSED'))
        )
        subscriber = UserBusSubscriber(
            connection, dict(_DEFAULT_CONFIG, uuid='origin-uuid'), _token(uuid='user-1')
        )

        with pytest.raises(ChannelClosed):
            await subscriber.__aenter__()

        assert connection._consumers == []
