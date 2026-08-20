# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock, sentinel
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
