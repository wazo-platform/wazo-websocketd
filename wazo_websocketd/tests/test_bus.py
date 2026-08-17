# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch, sentinel
from uuid import uuid4

import pytest
from aioamqp.exceptions import ChannelClosed
from xivo.auth_verifier import AccessCheck

from ..bus import BusConnection, BusConsumer, BusMessage
from ..config import _DEFAULT_CONFIG
from ..exception import (
    AuthenticationError,
    BusConnectionLostError,
    EventPermissionError,
    InvalidEvent,
    SessionRevokedError,
)


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
    return BusConsumer(Mock(), config, _token(**metadata))


def _properties(**headers):
    return Mock(headers=headers)


class TestBusDecoding:
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

        assert event == BusMessage('foo', properties.headers, None, {}, '{}')

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


class TestBusDispatching:
    def setup_method(self):
        self.consumer = _consumer()
        self.consumer._access = Mock(AccessCheck)

    async def test_a_lost_connection_is_raised_to_whoever_is_consuming(self):
        await self.consumer.connection_lost()

        with pytest.raises(BusConnectionLostError):
            async for _ in self.consumer:
                pass

    async def test_a_queued_event_is_handed_to_the_consumer(self):
        event = BusMessage(
            'foo', sentinel.headers, 'some.acl', sentinel.payload, sentinel.content
        )

        await self.consumer._queue.put(event)

        async for message in self.consumer:
            assert message == event
            break


class TestBusBindings:
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


class _FakeChannel:
    def __init__(self, fail_bind_with=None):
        self._fail_bind_with = fail_bind_with
        self.bind_arguments = []

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

    async def queue_bind(
        self, queue, exchange, routing_key, no_wait=False, arguments=None
    ):
        if self._fail_bind_with is not None:
            raise self._fail_bind_with
        self.bind_arguments.append(arguments)


def _revocable_consumer(**token_fields):
    token = _token(uuid='user-1')
    token['session_uuid'] = 'session-1'
    token.update(token_fields)
    return BusConsumer(Mock(), dict(_DEFAULT_CONFIG, uuid='origin-uuid'), token)


def _consumer_over(*channels):
    consumer = _revocable_consumer()
    opened = list(channels)
    consumer._connection = Mock(
        get_channel=AsyncMock(side_effect=lambda **_: opened.pop(0))
    )
    return consumer


async def _deliver(consumer, name, body, acl='some.acl'):
    channel = Mock(basic_client_ack=AsyncMock())
    properties = Mock(headers={'name': name, 'required_acl': acl})
    envelope = Mock(delivery_tag='tag')
    await consumer._on_message(channel, json.dumps(body).encode(), envelope, properties)


class TestSessionRevocation:
    async def test_the_deletion_of_our_own_session_terminates_the_consumer(self):
        consumer = _revocable_consumer()

        # enforcement must not depend on the client being allowed to see the event
        await _deliver(
            consumer,
            'auth_session_deleted',
            {'data': {'uuid': 'session-1'}},
            acl='nope',
        )

        with pytest.raises(SessionRevokedError):
            await consumer.__anext__()

    async def test_the_deletion_of_another_session_is_ignored(self):
        consumer = _revocable_consumer()

        await _deliver(consumer, 'auth_session_deleted', {'data': {'uuid': 'other'}})

        assert consumer._queue.empty()

    async def test_renewing_the_token_follows_the_new_session(self):
        consumer = _revocable_consumer()
        renewed = _token(uuid='user-1')
        renewed['session_uuid'] = 'session-9'

        consumer.set_token(renewed)
        await _deliver(
            consumer, 'auth_session_deleted', {'data': {'uuid': 'session-9'}}
        )

        with pytest.raises(SessionRevokedError):
            await consumer.__anext__()

    def test_a_token_for_another_user_is_refused(self):
        consumer = _revocable_consumer()
        other_user = _token(uuid='user-2')

        # every binding is keyed on the user, so the socket cannot change hands
        with pytest.raises(AuthenticationError):
            consumer.set_token(other_user)


class TestEventFiltering:
    async def test_an_event_nobody_subscribed_to_is_not_forwarded(self):
        consumer = _revocable_consumer()

        await _deliver(consumer, 'some_event', {'data': {}})

        assert consumer._queue.empty()

    async def test_a_subscribed_event_is_forwarded(self):
        consumer = _revocable_consumer()
        consumer._subscriptions.add('some_event')

        await _deliver(consumer, 'some_event', {'data': {}})

        assert (await consumer.__anext__()).name == 'some_event'

    async def test_a_wildcard_subscription_forwards_everything(self):
        consumer = _revocable_consumer()
        consumer._subscriptions.add('*')

        await _deliver(consumer, 'some_event', {'data': {}})

        assert (await consumer.__anext__()).name == 'some_event'


class TestConsumerSetup:
    async def test_a_tenant_exchange_that_vanished_mid_setup_is_redeclared(self):
        # the exchange is auto_delete, so the tenant's last session leaving
        # between our declare and our bind takes it with them
        gone = ChannelClosed(404, "NOT_FOUND - no exchange 'wazo-websocketd.tenant-x'")
        first, second = _FakeChannel(fail_bind_with=gone), _FakeChannel()
        consumer = _consumer_over(first, second)

        await consumer._start_consuming()

        assert consumer._channel is second
        assert second.bind_arguments == [
            {'name': 'auth_session_deleted', 'user_uuid:user-1': True}
        ]

    async def test_setup_gives_up_after_repeated_losses(self):
        gone = ChannelClosed(404, 'NOT_FOUND')
        attempts = BusConsumer._SETUP_RETRIES + 1
        consumer = _consumer_over(
            *(_FakeChannel(fail_bind_with=gone) for _ in range(attempts))
        )

        with patch.object(BusConsumer, '_SETUP_DELAY', 0):
            with pytest.raises(ChannelClosed):
                await consumer._start_consuming()

    async def test_a_refused_binding_is_not_retried(self):
        refused = ChannelClosed(403, 'ACCESS_REFUSED')
        consumer = _consumer_over(_FakeChannel(fail_bind_with=refused), _FakeChannel())

        with pytest.raises(ChannelClosed) as raised:
            await consumer._start_consuming()

        assert raised.value.code == 403

    async def test_a_consumer_whose_setup_failed_is_off_the_connection(self):
        connection = BusConnection('amqp://localhost')
        connection.get_channel = AsyncMock(  # type: ignore[method-assign]
            return_value=_FakeChannel(fail_bind_with=ChannelClosed(403, 'REFUSED'))
        )
        consumer = connection.spawn_consumer(
            dict(_DEFAULT_CONFIG, uuid='origin-uuid'), _token(uuid='user-1')
        )

        with pytest.raises(ChannelClosed):
            await consumer.__aenter__()
        await connection._notify_closed()

        # a leaked consumer would be handed every later connection loss
        assert consumer._queue.empty()
