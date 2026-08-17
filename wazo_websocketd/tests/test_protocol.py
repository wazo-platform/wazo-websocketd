# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import json

import pytest

from ..exception import SessionProtocolError
from ..protocol import SessionProtocolDecoder, SessionProtocolEncoder


class TestProtocolEncoder:
    def setup_method(self):
        self.encoder = SessionProtocolEncoder()

    def test_encode_init(self):
        encoded = json.loads(self.encoder.encode_init())

        assert encoded == {'op': 'init', 'code': 0, 'data': {'version': 2}}

    def test_encode_subscribe(self):
        encoded = json.loads(self.encoder.encode_subscribe())

        assert encoded == {'op': 'subscribe', 'code': 0, 'data': None}

    def test_encode_start(self):
        encoded = json.loads(self.encoder.encode_start())

        assert encoded == {'op': 'start', 'code': 0, 'data': None}

    def test_encode_pong(self):
        encoded = json.loads(self.encoder.encode_pong('abcd'))

        assert encoded == {'op': 'pong', 'code': 0, 'data': {'payload': 'abcd'}}

    def test_encode_event(self):
        event = {
            'name': 'auth_session_created',
            'origin_uuid': '2170f276-9344-44e8-aad7-dd98bb849b8f',
            'required_acl': 'events.auth.sessions.a725625b-01d0-4afb-a2de-dcbaa19031e5.created',  # noqa: E501
            'data': {
                'uuid': 'a725625b-01d0-4afb-a2de-dcbaa19031e5',
                'tenant_uuid': '47bfdafc-2897-4369-8fb3-153d41fb835d',
                'user_uuid': '73cfa622-6f5b-4a0d-9788-ddb72ab57836',
                'mobile': False,
            },
        }

        encoded = json.loads(self.encoder.encode_event(event))

        assert encoded == {'op': 'event', 'code': 0, 'data': event}


class TestProtocolDecoder:
    def setup_method(self):
        self.decoder = SessionProtocolDecoder()

    @pytest.mark.parametrize(
        'payload',
        [
            pytest.param(b'invalid', id='wrong type'),
            pytest.param('{invalid', id='invalid json'),
            pytest.param('1', id='root is not an object'),
            pytest.param('{}', id='missing op'),
            pytest.param('{"op": 2}', id='op is not a string'),
            pytest.param('{"op": "subscribe"}', id='subscribe without data'),
            pytest.param(
                '{"op": "subscribe", "data": 2}', id='subscribe data not an object'
            ),
            pytest.param(
                '{"op": "subscribe", "data": {}}', id='subscribe without event'
            ),
            pytest.param(
                '{"op": "subscribe", "data": {"event_name": 1}}',
                id='subscribe event not a string',
            ),
        ],
    )
    def test_a_malformed_message_is_refused(self, payload):
        with pytest.raises(SessionProtocolError):
            self.decoder.decode(payload)

    def test_decode_unknown_message(self):
        assert self.decoder.decode('{"op": "foo"}').op == 'foo'

    def test_decode_start(self):
        assert self.decoder.decode('{"op": "start"}').op == 'start'

    def test_decode_subscribe(self):
        message = self.decoder.decode(
            '{"op": "subscribe", "data": {"event_name": "foo"}}'
        )

        assert message.op == 'subscribe'
        assert message.value == 'foo'

    def test_decode_token(self):
        token = 'bc9571dd-bc62-4044-b78f-0bfb8a1481e4'

        message = self.decoder.decode(
            f'{{"op": "token", "data": {{"token": "{token}"}}}}'
        )

        assert message.op == 'token'
        assert message.value == token

    def test_decode_ping(self):
        message = self.decoder.decode('{"op": "ping", "data": {"payload": "abcd"}}')

        assert message.op == 'ping'
        assert message.value == 'abcd'
