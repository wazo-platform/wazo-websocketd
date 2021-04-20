# Copyright 2016-2021 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import unittest

from hamcrest import assert_that, equal_to, none, same_instance
from unittest.mock import Mock, sentinel
from xivo.auth_verifier import AccessCheck

from ..bus import _BusEvent, _decode_bus_msg
from ..exception import BusConnectionLostError
from ..session import EventTransmitter


class TestBusEvent(unittest.TestCase):
    def setUp(self):
        self.bus_msg = Mock()
        self.bus_msg.body = b'{}'

    def test_bus_msg(self):
        self.bus_msg.headers = {'name': 'foo', 'required_acl': 'some.acl'}

        bus_event = _decode_bus_msg(self.bus_msg)

        assert_that(bus_event.name, equal_to('foo'))
        assert_that(bus_event.has_acl, equal_to(True))
        assert_that(bus_event.acl, equal_to('some.acl'))
        assert_that(bus_event.msg_body, equal_to(self.bus_msg.body.decode('utf-8')))

    def test_bus_msg_none_required_acl(self):
        self.bus_msg.headers = {'name': 'foo', 'required_acl': None}

        bus_event = _decode_bus_msg(self.bus_msg)

        assert_that(bus_event.has_acl, equal_to(True))
        assert_that(bus_event.acl, none())

    def test_bus_msg_missing_required_acl(self):
        self.bus_msg.headers = {'name': 'foo'}

        bus_event = _decode_bus_msg(self.bus_msg)

        assert_that(bus_event.has_acl, equal_to(False))

    def test_bus_msg_missing_name(self):
        self.bus_msg.headers = {'required_acl': 'some.acl'}

        self.assertRaises(ValueError, _decode_bus_msg, self.bus_msg)

    def test_bus_msg_wrong_name_type(self):
        self.bus_msg.headers = {'name': None, 'required_acl': 'some.acl'}

        self.assertRaises(ValueError, _decode_bus_msg, self.bus_msg)

    def test_bus_msg_wrong_required_acl_type(self):
        self.bus_msg.headers = {'name': 'foo', 'required_acl': 2}

        self.assertRaises(ValueError, _decode_bus_msg, self.bus_msg)

    def test_bus_msg_invalid_type(self):
        self.bus_msg.body = b'{"name": "\xe8"}'

        self.assertRaises(ValueError, _decode_bus_msg, self.bus_msg)

    def test_bus_msg_invalid_json(self):
        self.bus_msg.body = b'{invalid'

        self.assertRaises(ValueError, _decode_bus_msg, self.bus_msg)

    def test_bus_msg_invalid_root_object_type(self):
        self.bus_msg.body = b'2'

        self.assertRaises(ValueError, _decode_bus_msg, self.bus_msg)


class TestBusEventTransmitter(unittest.TestCase):
    def setUp(self):
        self.bus_event = _new_bus_event('foo', acl='some.thing')
        self.event_transmitter = EventTransmitter()
        self.event_transmitter._access_check = Mock(AccessCheck)

    def test_get_connection_lost(self):
        self.event_transmitter.put_connection_lost()

        self.assertRaises(
            BusConnectionLostError,
            asyncio.get_event_loop().run_until_complete,
            self.event_transmitter.get(),
        )

    def test_get_event_subscribed_name(self):
        self.event_transmitter.subscribe_to_event(self.bus_event.name)
        self.event_transmitter.put(self.bus_event)
        result = asyncio.get_event_loop().run_until_complete(
            self.event_transmitter.get()
        )

        assert_that(result, same_instance(self.bus_event))

    def test_get_event_subscribed_star(self):
        self.event_transmitter.subscribe_to_event('*')
        self.event_transmitter.put(self.bus_event)
        result = asyncio.get_event_loop().run_until_complete(
            self.event_transmitter.get()
        )

        assert_that(result, same_instance(self.bus_event))

    def test_get_event_not_subscribed(self):
        self.event_transmitter.subscribe_to_event('some_unknown_event_name')
        self.event_transmitter.put(self.bus_event)

        assert_that(self.event_transmitter._queue.empty())

    def test_get_event_non_matching_acl(self):
        self.event_transmitter._access_check.matches_required_access.return_value = (
            False
        )

        self.event_transmitter.subscribe_to_event(self.bus_event.name)
        self.event_transmitter.put(self.bus_event)

        self.event_transmitter._access_check.matches_required_access.assert_called_once_with(
            self.bus_event.acl
        )
        assert_that(self.event_transmitter._queue.empty())


def _new_bus_event(name, has_acl=True, acl=None):
    return _BusEvent(name, has_acl, acl, sentinel.msg_body, sentinel.body)
