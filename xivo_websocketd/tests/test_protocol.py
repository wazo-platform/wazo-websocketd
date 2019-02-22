# Copyright 2016-2017 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import unittest

from hamcrest import assert_that, equal_to

from xivo_websocketd.protocol import SessionProtocolEncoder,\
    SessionProtocolDecoder
from xivo_websocketd.exception import SessionProtocolError


class TestProtocolEncoder(unittest.TestCase):

    def setUp(self):
        self.encoder = SessionProtocolEncoder()

    def test_encode_init(self):
        expected = {
            'op': 'init',
            'code': 0,
            'msg': '',
        }

        data = self.encoder.encode_init()

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_subscribe(self):
        expected = {
            'op': 'subscribe',
            'code': 0,
            'msg': '',
        }

        data = self.encoder.encode_subscribe()

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_start(self):
        expected = {
            'op': 'start',
            'code': 0,
            'msg': '',
        }

        data = self.encoder.encode_start()

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_set_presence(self):
        expected = {
            'op': 'set_presence',
            'code': 0,
            'msg': '',
        }

        data = self.encoder.encode_set_presence()

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_set_presence_unauthorized(self):
        expected = {
            'op': 'set_presence',
            'code': 401,
            'msg': 'unauthorized',
        }

        data = self.encoder.encode_set_presence_unauthorized()

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_set_presence_error(self):
        expected = {
            'op': 'set_presence',
            'code': 503,
            'msg': 'error',
        }

        data = self.encoder.encode_set_presence_error('error')

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_get_presence(self):
        expected = {
            'op': 'get_presence',
            'code': 0,
            'msg': {'user_uuid': '123',
                    'presence': 'dnd'},
        }

        data = self.encoder.encode_get_presence('123', 'dnd')

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_get_presence_unauthorized(self):
        expected = {
            'op': 'get_presence',
            'code': 401,
            'msg': 'unauthorized',
        }

        data = self.encoder.encode_get_presence_unauthorized()

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_get_presence_error(self):
        expected = {
            'op': 'get_presence',
            'code': 503,
            'msg': 'error',
        }

        data = self.encoder.encode_get_presence_error('error')

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
        assert_that(msg.event_name, equal_to('foo'))

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

    def test_decode_set_presence(self):
        data = '{"op": "set_presence", "data": {"user_uuid": "123", "presence": "dnd"}}'

        msg = self.decoder.decode(data)

        assert_that(msg.op, equal_to('set_presence'))
        assert_that(msg.user_uuid, equal_to('123'))
        assert_that(msg.presence, equal_to('dnd'))

    def test_decode_get_presence(self):
        data = '{"op": "get_presence", "data": {"user_uuid": "123"}}'

        msg = self.decoder.decode(data)

        assert_that(msg.op, equal_to('get_presence'))
        assert_that(msg.user_uuid, equal_to('123'))

    def test_decode_start(self):
        data = '{"op": "start"}'

        msg = self.decoder.decode(data)

        assert_that(msg.op, equal_to('start'))
