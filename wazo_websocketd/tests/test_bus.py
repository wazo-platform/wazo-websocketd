# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import AsyncMock, Mock, sentinel
from uuid import uuid4

import pytest
from xivo.auth_verifier import AccessCheck

from ..bus import BusConsumer, BusMessage
from ..config import _DEFAULT_CONFIG
from ..exception import BusConnectionLostError, EventPermissionError, InvalidEvent


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


def _wired(consumer):
    consumer._channel = AsyncMock()
    consumer._amqp_queue = sentinel.queue
    consumer._bound_exchange = sentinel.exchange
    return consumer


def _calls(method):
    calls = []
    for call in method.await_args_list:
        assert call.args == (sentinel.queue, sentinel.exchange, '')
        calls.append(call.kwargs['arguments'])
    return calls


def _bound(consumer):
    return _calls(consumer._channel.queue_bind)


def _unbound(consumer):
    return _calls(consumer._channel.queue_unbind)


def _own(user_uuid):
    return {f'user_uuid:{user_uuid}': True}


def _broadcast(name):
    return {'name': name, 'user_uuid:*': True}


EVENT_NAMES = [f'event_{n}' for n in range(17)]


class TestBusSubscribedNameFilter:
    def setup_method(self):
        self.consumer = _wired(_consumer(purpose='user', admin=False))
        self.consumer._access = Mock(AccessCheck)
        self.consumer._access.matches_required_access.return_value = True

    async def _deliver(self, content=b'{}', **headers):
        headers.setdefault('required_acl', 'some.acl')
        envelope = Mock(delivery_tag=sentinel.tag)
        await self.consumer._on_message(
            self.consumer._channel, content, envelope, _properties(**headers)
        )
        self.consumer._channel.basic_client_ack.assert_awaited_with(
            sentinel.tag, multiple=True
        )

    def _queued(self):
        queued = []
        while not self.consumer._queue.empty():
            queued.append(self.consumer._queue.get_nowait().name)
        return queued

    async def test_a_message_of_a_subscribed_name_is_queued(self):
        await self.consumer.bind('foo')

        await self._deliver(name='foo')

        assert self._queued() == ['foo']

    async def test_a_message_of_another_name_is_acked_and_dropped(self):
        await self.consumer.bind('foo')

        await self._deliver(name='bar')

        assert self._queued() == []

    async def test_a_dropped_message_is_logged_by_its_name_only(self, caplog):
        await self.consumer.bind('foo')

        with caplog.at_level(logging.DEBUG, logger='wazo_websocketd.bus'):
            await self._deliver(content=b'{"secret": "x"}', name='bar')

        assert [r.getMessage() for r in caplog.records] == [
            'discarding event `bar` (not subscribed)'
        ]

    async def test_a_message_named_only_in_its_payload_is_acked_and_dropped(self):
        await self.consumer.bind('foo')

        await self._deliver(content=b'{"name": "foo"}')

        assert self._queued() == []

    async def test_a_message_with_no_headers_at_all_is_acked_and_dropped(self):
        await self.consumer.bind('foo')
        envelope = Mock(delivery_tag=sentinel.tag)

        await self.consumer._on_message(
            self.consumer._channel, b'{}', envelope, Mock(headers=None)
        )

        self.consumer._channel.basic_client_ack.assert_awaited_with(
            sentinel.tag, multiple=True
        )
        assert self._queued() == []

    async def test_every_message_is_queued_once_star_is_subscribed(self):
        await self.consumer.bind('foo')
        await self.consumer.bind('*')

        await self._deliver(name='foo')
        await self._deliver(name='bar')
        await self._deliver(content=b'{"name": "baz"}')

        assert self._queued() == ['foo', 'bar', 'baz']

    async def test_nothing_is_queued_before_any_subscription(self):
        await self._deliver(name='foo')

        assert self._queued() == []

    async def test_an_admin_drops_a_name_it_did_not_bind(self):
        self.consumer.set_token(_token(purpose='user', admin=True))
        self.consumer._access = Mock(AccessCheck)
        await self.consumer.bind('foo')

        await self._deliver(name='foo')
        await self._deliver(name='bar')

        assert self._queued() == ['foo']

    async def test_a_user_turned_admin_still_drops_an_unbound_name(self):
        for name in EVENT_NAMES:
            await self.consumer.bind(name)
        self.consumer.set_token(_token(purpose='user', admin=True))
        self.consumer._access = Mock(AccessCheck)

        await self._deliver(name='event_17')
        await self._deliver(name='event_16')

        assert self._queued() == ['event_16']

    async def test_a_user_turned_another_user_still_drops_an_unbound_name(self):
        first_uuid = self.consumer._user.uuid
        await self.consumer.bind('foo')
        second_uuid = str(uuid4())
        self.consumer.set_token(_token(purpose='user', admin=False, uuid=second_uuid))
        self.consumer._access = Mock(AccessCheck)

        await self._deliver(name='bar', **{f'user_uuid:{first_uuid}': True})
        await self._deliver(name='bar', **{f'user_uuid:{second_uuid}': True})
        await self._deliver(name='foo', **{f'user_uuid:{second_uuid}': True})

        assert self._queued() == ['foo']

    async def test_a_name_header_that_is_not_a_string_is_acked_and_dropped(self):
        await self.consumer.bind('foo')

        await self._deliver(name={'foo': 'bar'})
        await self._deliver(name=['foo'])

        assert self._queued() == []

    async def test_a_subscribed_name_still_needs_the_acl(self):
        self.consumer._access.matches_required_access.return_value = False
        await self.consumer.bind('foo')

        await self._deliver(name='foo', required_acl='event.foo')

        assert self._queued() == []
        self.consumer._access.matches_required_access.assert_called_once_with(
            'event.foo'
        )


class TestBusBindCalls:
    async def test_a_user_binds_its_own_events_once_and_each_name_broadcast(self):
        user_uuid = str(uuid4())
        consumer = _wired(_consumer(purpose='user', admin=False, uuid=user_uuid))

        for name in EVENT_NAMES:
            await consumer.bind(name)

        assert _bound(consumer) == [_own(user_uuid)] + [
            _broadcast(name) for name in EVENT_NAMES
        ]

    async def test_binding_a_name_twice_binds_nothing_more(self):
        user_uuid = str(uuid4())
        consumer = _wired(_consumer(purpose='user', admin=False, uuid=user_uuid))

        await consumer.bind('foo')
        await consumer.bind('foo')

        assert _bound(consumer) == [_own(user_uuid), _broadcast('foo')]

    async def test_star_then_a_name_binds_only_the_name_broadcast(self):
        user_uuid = str(uuid4())
        consumer = _wired(_consumer(purpose='user', admin=False, uuid=user_uuid))

        await consumer.bind('*')
        await consumer.bind('foo')

        assert _bound(consumer) == [
            _own(user_uuid),
            {'user_uuid:*': True},
            _broadcast('foo'),
        ]

    async def test_names_then_star_binds_todays_two_star_bindings(self):
        user_uuid = str(uuid4())
        consumer = _wired(_consumer(purpose='user', admin=False, uuid=user_uuid))

        await consumer.bind('foo')
        await consumer.bind('*')

        assert _bound(consumer) == [
            _own(user_uuid),
            _broadcast('foo'),
            _own(user_uuid),
            {'user_uuid:*': True},
        ]

    async def test_star_binds_as_today(self):
        user_uuid = str(uuid4())
        consumer = _wired(_consumer(purpose='user', admin=False, uuid=user_uuid))

        await consumer.bind('*')

        assert _bound(consumer) == [_own(user_uuid), {'user_uuid:*': True}]

    async def test_an_admin_binds_as_today_and_records_its_names(self):
        origin_uuid = Mock()
        consumer = _wired(
            _consumer(origin_uuid=origin_uuid, purpose='user', admin=True)
        )

        await consumer.bind('foo')
        await consumer.bind('*')

        assert _bound(consumer) == [
            {'name': 'foo', 'origin_uuid': origin_uuid},
            {'origin_uuid': origin_uuid},
        ]
        assert consumer._event_names == {'foo', '*'}

    async def test_a_bind_after_a_uuid_change_binds_the_new_uuids_own_events(self):
        first_uuid, second_uuid = str(uuid4()), str(uuid4())
        consumer = _wired(_consumer(purpose='user', admin=False, uuid=first_uuid))

        await consumer.bind('foo')
        consumer.set_token(_token(purpose='user', admin=False, uuid=second_uuid))
        await consumer.bind('bar')

        assert _bound(consumer) == [
            _own(first_uuid),
            _broadcast('foo'),
            _own(second_uuid),
            _broadcast('bar'),
        ]

    async def test_a_bind_after_admin_turned_user_binds_the_users_own_events(self):
        origin_uuid = Mock()
        user_uuid = str(uuid4())
        consumer = _wired(
            _consumer(origin_uuid=origin_uuid, purpose='user', admin=True)
        )

        await consumer.bind('foo')
        consumer.set_token(_token(purpose='user', admin=False, uuid=user_uuid))
        await consumer.bind('bar')

        assert _bound(consumer) == [
            {'name': 'foo', 'origin_uuid': origin_uuid},
            _own(user_uuid),
            _broadcast('bar'),
        ]

    async def test_a_bind_after_admin_turned_user_of_the_same_uuid_binds_its_own_events(
        self,
    ):
        origin_uuid = Mock()
        user_uuid = str(uuid4())
        consumer = _wired(
            _consumer(
                origin_uuid=origin_uuid, purpose='user', admin=True, uuid=user_uuid
            )
        )

        await consumer.bind('foo')
        consumer.set_token(_token(purpose='user', admin=False, uuid=user_uuid))
        await consumer.bind('bar')

        assert _bound(consumer) == [
            {'name': 'foo', 'origin_uuid': origin_uuid},
            _own(user_uuid),
            _broadcast('bar'),
        ]

    async def test_a_name_bound_again_after_a_uuid_change_binds_the_new_own_events(
        self,
    ):
        first_uuid, second_uuid = str(uuid4()), str(uuid4())
        consumer = _wired(_consumer(purpose='user', admin=False, uuid=first_uuid))

        await consumer.bind('foo')
        consumer.set_token(_token(purpose='user', admin=False, uuid=second_uuid))
        await consumer.bind('foo')

        assert _bound(consumer) == [
            _own(first_uuid),
            _broadcast('foo'),
            _own(second_uuid),
        ]

    async def test_a_name_bound_again_after_admin_turned_user_binds_the_users_bindings(
        self,
    ):
        origin_uuid = Mock()
        user_uuid = str(uuid4())
        consumer = _wired(
            _consumer(origin_uuid=origin_uuid, purpose='user', admin=True)
        )

        await consumer.bind('foo')
        consumer.set_token(_token(purpose='user', admin=False, uuid=user_uuid))
        await consumer.bind('foo')

        assert _bound(consumer) == [
            {'name': 'foo', 'origin_uuid': origin_uuid},
            _own(user_uuid),
            _broadcast('foo'),
        ]


class TestBusUnbindCalls:
    def setup_method(self):
        self.user_uuid = str(uuid4())
        self.consumer = _wired(
            _consumer(purpose='user', admin=False, uuid=self.user_uuid)
        )

    async def test_unbinding_a_name_keeps_the_own_events_binding(self):
        await self.consumer.bind('foo')
        await self.consumer.bind('bar')

        await self.consumer.unbind('foo')

        assert _unbound(self.consumer) == [_broadcast('foo')]

    async def test_unbinding_the_last_name_removes_the_own_events_binding(self):
        await self.consumer.bind('foo')

        await self.consumer.unbind('foo')

        assert _unbound(self.consumer) == [_broadcast('foo'), _own(self.user_uuid)]

    async def test_unbinding_star_with_names_left_removes_only_its_broadcast(self):
        await self.consumer.bind('foo')
        await self.consumer.bind('*')

        await self.consumer.unbind('*')

        assert _unbound(self.consumer) == [{'user_uuid:*': True}]

    async def test_unbinding_names_and_star_removes_the_own_events_binding_once(self):
        await self.consumer.bind('foo')
        await self.consumer.bind('bar')
        await self.consumer.bind('*')

        await self.consumer.unbind('foo')
        await self.consumer.unbind('*')
        await self.consumer.unbind('bar')

        assert _unbound(self.consumer) == [
            _broadcast('foo'),
            {'user_uuid:*': True},
            _broadcast('bar'),
            _own(self.user_uuid),
        ]

    async def test_unbinding_the_last_name_removes_every_own_events_binding_made(self):
        second_uuid = str(uuid4())
        await self.consumer.bind('foo')
        self.consumer.set_token(_token(purpose='user', admin=False, uuid=second_uuid))
        await self.consumer.bind('bar')

        await self.consumer.unbind('foo')
        await self.consumer.unbind('bar')

        unbound = _unbound(self.consumer)
        assert unbound[:2] == [_broadcast('foo'), _broadcast('bar')]
        assert sorted(unbound[2:], key=str) == sorted(
            [_own(self.user_uuid), _own(second_uuid)], key=str
        )

    async def test_a_bind_after_the_last_unbind_binds_the_own_events_again(self):
        await self.consumer.bind('foo')
        await self.consumer.unbind('foo')

        await self.consumer.bind('bar')

        assert _bound(self.consumer) == [
            _own(self.user_uuid),
            _broadcast('foo'),
            _own(self.user_uuid),
            _broadcast('bar'),
        ]

    async def test_unbinding_an_unknown_name_calls_nothing(self):
        await self.consumer.bind('foo')

        await self.consumer.unbind('bar')

        assert _unbound(self.consumer) == []
        await self.consumer.unbind('bar')
        assert _unbound(self.consumer) == []

    async def test_an_admin_unbinding_an_unknown_name_calls_nothing(self):
        consumer = _wired(_consumer(purpose='user', admin=True))
        await consumer.bind('foo')

        await consumer.unbind('bar')

        assert _unbound(consumer) == []

    async def test_a_user_unbinding_a_name_it_bound_as_admin_calls_nothing(self):
        consumer = _wired(_consumer(purpose='user', admin=True))
        await consumer.bind('foo')
        await consumer.bind('bar')
        consumer.set_token(_token(purpose='user', admin=False))

        await consumer.unbind('foo')

        assert _unbound(consumer) == []

    async def test_an_admin_unbinding_a_name_removes_its_origin_binding(self):
        origin_uuid = Mock()
        consumer = _wired(
            _consumer(origin_uuid=origin_uuid, purpose='user', admin=True)
        )
        await consumer.bind('foo')
        await consumer.bind('bar')

        await consumer.unbind('foo')

        assert _unbound(consumer) == [{'name': 'foo', 'origin_uuid': origin_uuid}]


class TestBusBindings:
    def test_a_user_binds_to_its_own_events_and_to_the_broadcasts(self):
        user_uuid = str(uuid4())
        consumer = _consumer(purpose='user', admin=False, uuid=user_uuid)

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
