# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import json
import unittest

from hamcrest import assert_that, equal_to

from xivo_websocketd.protocol import _SessionProtocolEncoder,\
    _SessionProtocolDecoder
from xivo_websocketd.exception import SessionProtocolError


class TestProtocolEncoder(unittest.TestCase):

    def setUp(self):
        self.encoder = _SessionProtocolEncoder()

    def test_encode_bind_success(self):
        expected = {
            'op': 'bind',
            'code': 0,
            'msg': '',
        }

        data = self.encoder.encode_bind(True)

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_bind_no_success(self):
        expected = {
            'op': 'bind',
            'code': 1,
            'msg': 'unknown exchange',
        }

        data = self.encoder.encode_bind(False)

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_init(self):
        expected = {
            'op': 'init',
            'code': 0,
            'msg': '',
        }

        data = self.encoder.encode_init()

        assert_that(json.loads(data), equal_to(expected))

    def test_encode_start(self):
        expected = {
            'op': 'start',
            'code': 0,
            'msg': '',
        }

        data = self.encoder.encode_start()

        assert_that(json.loads(data), equal_to(expected))


class TestProtocolDecoder(unittest.TestCase):

    def setUp(self):
        self.decoder = _SessionProtocolDecoder()

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

    def test_decode_bind(self):
        data = '{"op": "bind", "data": {"exchange_name": "foo", "routing_key": "bar.*"}}'

        msg = self.decoder.decode(data)

        assert_that(msg.op, equal_to('bind'))
        assert_that(msg.exchange_name, equal_to('foo'))
        assert_that(msg.routing_key, equal_to('bar.*'))

    def test_decode_start(self):
        data = '{"op": "start"}'

        msg = self.decoder.decode(data)

        assert_that(msg.op, equal_to('start'))
