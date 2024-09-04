# Copyright 2016-2024 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from unittest.mock import Mock, sentinel
from uuid import uuid4

import pytest
from hamcrest import (
    assert_that,
    calling,
    contains_inanyorder,
    equal_to,
    has_entries,
    raises,
)
from wazo_auth_client.types import TokenDict
from xivo.auth_verifier import AccessCheck

from ..bus import BusConsumer, BusMessage
from ..config import _DEFAULT_CONFIG
from ..exception import BusConnectionLostError, EventPermissionError, InvalidEvent


@pytest.fixture
def mock_token_fixture(request: pytest.FixtureRequest) -> None:
    request.cls.mock_token = {
        'token': f'{uuid4()}',
        'session_uuid': f'{uuid4()}',
        'acl': ['some.acl'],
        'metadata': {
            'uuid': f'{uuid4()}',
            'tenant_uuid': f'{uuid4()}',
            'auth_id': f'{uuid4()}',
            'pbx_user_uuid': f'{uuid4()}',
            'xivo_uuid': f'{uuid4()}',
        },
        'utc_expires_at': f'{datetime.now()}',
        'expires_at': f'{datetime.utcnow()}',
        'issued_at': f'{datetime.now()}',
        'utc_issued_at': f'{datetime.utcnow()}',
        'user_agent': 'some-user-agent',
        'remote_addr': '127.0.0.1',
        'xivo_uuid': f'{uuid4()}',
        'auth_id': f'{uuid4()}',
    }


@pytest.mark.usefixtures('mock_token_fixture')
class TestBusDecoding(unittest.TestCase):
    mock_token: TokenDict

    def setUp(self):
        mock_config = dict(_DEFAULT_CONFIG, uuid=Mock())
        self.consumer = BusConsumer(Mock(), mock_config, self.mock_token)

    def test_bus_msg(self):
        message = b'{}'
        properties = Mock(
            headers={
                'name': 'foo',
                'required_acl': 'some.acl',
            },
        )

        event = self.consumer._decode_content(message, properties)

        assert_that(event.name, equal_to('foo'))
        assert_that(event.acl, equal_to('some.acl'))
        assert_that(event.content, equal_to({}))
        assert_that(event.raw, equal_to(message.decode('utf-8')))

    def test_bus_msg_none_required_acl(self):
        message = b'{}'
        properties = Mock(
            headers={
                'name': 'foo',
                'required_acl': None,
            }
        )

        assert_that(
            self.consumer._decode_content(message, properties),
            equal_to(
                BusMessage('foo', properties.headers, None, {}, message.decode('utf-8'))
            ),
        )

    def test_bus_msg_missing_required_acl(self):
        message = b'{}'
        properties = Mock(
            headers={
                'name': 'foo',
            }
        )

        assert_that(
            calling(self.consumer._decode_content).with_args(message, properties),
            raises(EventPermissionError),
        )

    def test_bus_msg_missing_name(self):
        message = b'{}'
        properties = Mock(
            headers={
                'required_acl': 'some.acl',
            }
        )

        assert_that(
            calling(self.consumer._decode_content).with_args(message, properties),
            raises(InvalidEvent),
        )

    def test_bus_msg_wrong_name_type(self):
        message = b'{}'
        properties = Mock(
            headers={
                'name': None,
                'required_acl': 'some.acl',
            }
        )

        assert_that(
            calling(self.consumer._decode_content).with_args(message, properties),
            raises(InvalidEvent),
        )

    def test_bus_msg_wrong_required_acl_type(self):
        message = b'{}'
        properties = Mock(
            headers={
                'name': 'foo',
                'required_acl': 2,
            },
        )

        assert_that(
            calling(self.consumer._decode_content).with_args(message, properties),
            raises(InvalidEvent),
        )

    def test_bus_msg_invalid_type(self):
        message = b'{"name": "\xe8"}'
        properties = Mock(
            headers={
                'name': 'foo',
                'required_acl': 'some.acl',
            }
        )

        assert_that(
            calling(self.consumer._decode_content).with_args(message, properties),
            raises(InvalidEvent),
        )

    def test_bus_msg_invalid_json(self):
        message = b'{invalid'
        properties = Mock(
            headers={
                'name': 'foo',
                'required_acl': 'some.acl',
            }
        )

        assert_that(
            calling(self.consumer._decode_content).with_args(message, properties),
            raises(InvalidEvent),
        )

    def test_bus_msg_invalid_root_object_type(self):
        message = b'2'
        properties = Mock(
            headers={
                'name': 'foo',
                'required_acl': 'some.acl',
            }
        )

        assert_that(
            calling(self.consumer._decode_content).with_args(message, properties),
            raises(InvalidEvent),
        )


@pytest.mark.usefixtures('mock_token_fixture')
class TestBusDispatching(unittest.TestCase):
    mock_token: TokenDict

    def setUp(self):
        self.loop = asyncio.get_event_loop()
        self.event = BusMessage(
            'foo', sentinel.headers, 'some.acl', sentinel.payload, sentinel.content
        )
        mock_config = dict(_DEFAULT_CONFIG, uuid=Mock())

        self.consumer = BusConsumer(Mock(), mock_config, self.mock_token)
        self.consumer._access = Mock(AccessCheck)

    def test_connection_lost(self):
        async def consume():
            await self.consumer.connection_lost()

            async for _ in self.consumer:
                pass

        assert_that(
            calling(self.loop.run_until_complete).with_args(consume()),
            raises(BusConnectionLostError),
        )

    def test_receiving_event(self):
        async def consume():
            await self.consumer._queue.put(self.event)

            async for message in self.consumer:
                return message

        assert_that(
            self.loop.run_until_complete(consume()),
            equal_to(self.event),
        )


@pytest.mark.usefixtures('mock_token_fixture')
class TestBusBindings(unittest.TestCase):
    mock_token: TokenDict

    def setUp(self):
        self.origin_uuid = Mock()
        self.mock_config = dict(_DEFAULT_CONFIG, uuid=self.origin_uuid)

    def _make_consumer(
        self,
        purpose: str,
        admin: bool | None = None,
        user_uuid: str | None = None,
    ):
        self.mock_token['metadata']['uuid'] = user_uuid
        self.mock_token['metadata']['purpose'] = purpose
        if admin is not None:
            self.mock_token['metadata']['admin'] = admin

        return BusConsumer(Mock(), self.mock_config, self.mock_token)

    def test_user_bindings(self):
        user_uuid = str(uuid4())
        consumer = self._make_consumer(purpose='user', admin=False, user_uuid=user_uuid)
        assert_that(
            consumer._generate_bindings('some_event'),
            contains_inanyorder(
                has_entries({'name': 'some_event', f'user_uuid:{user_uuid}': True}),
                has_entries({'name': 'some_event', 'user_uuid:*': True}),
            ),
        )

        assert_that(
            consumer._generate_bindings('*'),
            contains_inanyorder(
                has_entries({f'user_uuid:{user_uuid}': True}),
                has_entries({'user_uuid:*': True}),
            ),
        )

    def test_admin_bindings(self):
        consumer = self._make_consumer(purpose='user', admin=True)
        assert_that(
            consumer._generate_bindings('some_event'),
            contains_inanyorder(
                has_entries(name='some_event', origin_uuid=self.origin_uuid),
            ),
        )

        assert_that(
            consumer._generate_bindings('*'),
            contains_inanyorder(
                has_entries(origin_uuid=self.origin_uuid),
            ),
        )

    def test_internal_user_bindings(self):
        consumer = self._make_consumer(purpose='internal')
        assert_that(
            consumer._generate_bindings('some_event'),
            contains_inanyorder(
                has_entries(name='some_event', origin_uuid=self.origin_uuid),
            ),
        )

        assert_that(
            consumer._generate_bindings('*'),
            contains_inanyorder(
                has_entries(origin_uuid=self.origin_uuid),
            ),
        )

    def test_extenal_api_bindings(self):
        consumer = self._make_consumer(purpose='external_api')
        assert_that(
            consumer._generate_bindings('some_event'),
            contains_inanyorder(
                has_entries(name='some_event', origin_uuid=self.origin_uuid),
            ),
        )

        assert_that(
            consumer._generate_bindings('*'),
            contains_inanyorder(
                has_entries(origin_uuid=self.origin_uuid),
            ),
        )
