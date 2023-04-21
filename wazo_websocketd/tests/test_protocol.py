# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import unittest

from hamcrest import assert_that, equal_to

from ..exception import SessionProtocolError
from ..protocol import SessionProtocolDecoder, SessionProtocolEncoder


class TestProtocolEncoder(unittest.TestCase):
    def setUp(self):
        self.encoder = SessionProtocolEncoder()

    def test_encode_init(self):
        expected = {'op': 'init', 'code': 0, 'data': {'version': 2}}

        data = self.encoder.encode_init()

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_subscribe(self):
        expected = {'op': 'subscribe', 'code': 0, 'data': None}

        data = self.encoder.encode_subscribe()

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_start(self):
        expected = {'op': 'start', 'code': 0, 'data': None}

        data = self.encoder.encode_start()

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_event(self):
        event = {
            "name": "auth_session_created",
            "origin_uuid": "2170f276-9344-44e8-aad7-dd98bb849b8f",
            "required_acl": "events.auth.sessions.a725625b-01d0-4afb-a2de-dcbaa19031e5.created",
            "data": {
                "uuid": "a725625b-01d0-4afb-a2de-dcbaa19031e5",
                "tenant_uuid": "47bfdafc-2897-4369-8fb3-153d41fb835d",
                "user_uuid": "73cfa622-6f5b-4a0d-9788-ddb72ab57836",
                "mobile": False,
            },
        }

        expected = {'op': 'event', 'code': 0, 'data': event}

        data = self.encoder.encode_event(event)

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_pong(self):
        expected = {'op': 'pong', 'code': 0, 'data': {'payload': 'abcd'}}

        data = self.encoder.encode_pong('abcd')

        assert_that(json.loads(data), equal_to(expected))


class TestProtocolDecoder(unittest.TestCase):
    def setUp(self):
        self.decoder = SessionProtocolDecoder()

    def test_decode_wrong_type(self):
        data = b'invalid'
        self.assertRaises(SessionProtocolError, self.decoder.decode, data)

    def test_decode_invalid_json(self):
        data = '{invalid'
        self.assertRaises(SessionProtocolError, self.decoder.decode, data)

    def test_decode_wrong_root_object_type(self):
        data = '1'
        self.assertRaises(SessionProtocolError, self.decoder.decode, data)

    def test_decode_missing_op_key(self):
        data = '{}'
        self.assertRaises(SessionProtocolError, self.decoder.decode, data)

    def test_decode_wrong_op_type(self):
        data = '{"op": 2}'
        self.assertRaises(SessionProtocolError, self.decoder.decode, data)

    def test_decode_unknown_message(self):
        data = '{"op": "foo"}'

        msg = self.decoder.decode(data)

        assert_that(msg.op, equal_to('foo'))

    def test_decode_subscribe(self):
        data = '{"op": "subscribe", "data": {"event_name": "foo"}}'

        msg = self.decoder.decode(data)

        assert_that(msg.op, equal_to('subscribe'))
        assert_that(msg.value, equal_to('foo'))

    def test_decode_subscribe_missing_data_key(self):
        data = '{"op": "subscribe"}'

        self.assertRaises(SessionProtocolError, self.decoder.decode, data)

    def test_decode_subscribe_invalid_data_type(self):
        data = '{"op": "subscribe", "data": 2}'

        self.assertRaises(SessionProtocolError, self.decoder.decode, data)

    def test_decode_subscribe_missing_event_name_key(self):
        data = '{"op": "subscribe", "data": {}}'

        self.assertRaises(SessionProtocolError, self.decoder.decode, data)

    def test_decode_subscribe_invalid_event_name_type(self):
        data = '{"op": "subscribe", "data": {"event_name": 1}}'

        self.assertRaises(SessionProtocolError, self.decoder.decode, data)

    def test_decode_start(self):
        data = '{"op": "start"}'

        msg = self.decoder.decode(data)

        assert_that(msg.op, equal_to('start'))

    def test_decode_token(self):
        token = "bc9571dd-bc62-4044-b78f-0bfb8a1481e4"
        data = f'{{"op": "token", "data": {{"token": "{token}"}}}}'

        msg = self.decoder.decode(data)

        assert_that(msg.op, equal_to('token'))
        assert_that(msg.value, equal_to(token))

    def test_decode_ping(self):
        data = '{"op": "ping", "data": {"payload": "abcd"}}'

        msg = self.decoder.decode(data)

        assert_that(msg.op, equal_to('ping'))
        assert_that(msg.value, equal_to('abcd'))
