# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import unittest

from unittest.mock import Mock, sentinel

from hamcrest import assert_that, equal_to, none, same_instance

from xivo_websocketd.acl import ACLCheck
from xivo_websocketd.bus import _BusEventConsumer, _BusEventDispatcher, \
    _decode_bus_msg, _BusEvent
from xivo_websocketd.exception import BusConnectionLostError


class TestBusEvent(unittest.TestCase):

    def setUp(self):
        self.bus_msg = Mock()

    def test_bus_msg(self):
        self.bus_msg.body = b'{"name": "foo", "required_acl": "some.acl"}'

        bus_event = _decode_bus_msg(self.bus_msg)

        assert_that(bus_event.name, equal_to('foo'))
        assert_that(bus_event.has_acl, equal_to(True))
        assert_that(bus_event.acl, equal_to('some.acl'))
        assert_that(bus_event.msg_body, equal_to(self.bus_msg.body.decode('utf-8')))

    def test_bus_msg_no_acl(self):
        self.bus_msg.body = b'{"name": "foo"}'

        bus_event = _decode_bus_msg(self.bus_msg)

        assert_that(bus_event.has_acl, equal_to(False))

    def test_bus_msg_null_required_acl(self):
        self.bus_msg.body = b'{"name": "foo", "required_acl": null}'

        bus_event = _decode_bus_msg(self.bus_msg)

        assert_that(bus_event.has_acl, equal_to(True))
        assert_that(bus_event.acl, none())

    def test_bus_msg_invalid_encoding(self):
        self.bus_msg.body = b'{"name": "\xe8"}'

        self.assertRaises(ValueError, _decode_bus_msg, self.bus_msg)

    def test_bus_msg_invalid_json(self):
        self.bus_msg.body = b'{invalid'

        self.assertRaises(ValueError, _decode_bus_msg, self.bus_msg)

    def test_bus_msg_invalid_root_object_type(self):
        self.bus_msg.body = b'2'

        self.assertRaises(ValueError, _decode_bus_msg, self.bus_msg)

    def test_bus_msg_missing_name(self):
        self.bus_msg.body = b'{}'

        self.assertRaises(ValueError, _decode_bus_msg, self.bus_msg)

    def test_bus_msg_wrong_name_type(self):
        self.bus_msg.body = b'{"name": null}'

        self.assertRaises(ValueError, _decode_bus_msg, self.bus_msg)

    def test_bus_msg_wrong_required_acl_type(self):
        self.bus_msg.body = b'{"name": "foo", "required_acl": 2}'

        self.assertRaises(ValueError, _decode_bus_msg, self.bus_msg)


class TestBusEventDispatcher(unittest.TestCase):

    def setUp(self):
        self.bus_event_dispatcher = _BusEventDispatcher()
        self.bus_event_consumer = Mock(_BusEventConsumer)

    def test_dispatch_connection_lost(self):
        self.bus_event_dispatcher.add_event_consumer(self.bus_event_consumer)
        self.bus_event_dispatcher.dispatch_connection_lost()

        self.bus_event_consumer._on_connection_lost.assert_called_once_with()

    def test_dispatch_event_with_acl(self):
        bus_event = _new_bus_event('foo', has_acl=True)

        self.bus_event_dispatcher.add_event_consumer(self.bus_event_consumer)
        self.bus_event_dispatcher.dispatch_event(bus_event)

        self.bus_event_consumer._on_event.assert_called_once_with(bus_event)

    def test_dispatch_event_without_acl(self):
        bus_event = _new_bus_event('foo', has_acl=False)

        self.bus_event_dispatcher.add_event_consumer(self.bus_event_consumer)
        self.bus_event_dispatcher.dispatch_event(bus_event)

        assert_that(self.bus_event_consumer._on_event.called, equal_to(False))

    def test_remove_event_consumer(self):
        self.bus_event_dispatcher.add_event_consumer(self.bus_event_consumer)
        self.bus_event_dispatcher.remove_event_consumer(self.bus_event_consumer)
        self.bus_event_dispatcher.dispatch_connection_lost()

        assert_that(self.bus_event_consumer._on_connection_lost.called, equal_to(False))


class TestBusEventConsumer(unittest.TestCase):

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.bus_event_dispatcher = Mock(_BusEventDispatcher)
        self.acl_check = Mock(ACLCheck)
        self.bus_event = _new_bus_event('foo', acl='some.thing')
        self.bus_event_consumer = _BusEventConsumer(self.loop, self.bus_event_dispatcher, self.acl_check)

    def test_close(self):
        self.bus_event_consumer.close()

        self.bus_event_dispatcher.remove_event_consumer.assert_called_once_with(self.bus_event_consumer)

    def test_get_connection_lost(self):
        self.bus_event_consumer._on_connection_lost()

        self.assertRaises(BusConnectionLostError, self.loop.run_until_complete, self.bus_event_consumer.get())

    def test_get_event_subscribed_name(self):
        self.bus_event_consumer.subscribe_to_event(self.bus_event.name)
        self.bus_event_consumer._on_event(self.bus_event)
        result = self.loop.run_until_complete(self.bus_event_consumer.get())

        assert_that(result, same_instance(self.bus_event))

    def test_get_event_subscribed_star(self):
        self.bus_event_consumer.subscribe_to_event('*')
        self.bus_event_consumer._on_event(self.bus_event)
        result = self.loop.run_until_complete(self.bus_event_consumer.get())

        assert_that(result, same_instance(self.bus_event))

    def test_get_event_not_subscribed(self):
        self.bus_event_consumer.subscribe_to_event('some_unknown_event_name')
        self.bus_event_consumer._on_event(self.bus_event)

        assert_that(self.bus_event_consumer._queue.empty())

    def test_get_event_non_matching_acl(self):
        self.acl_check.matches_required_acl.return_value = False

        self.bus_event_consumer.subscribe_to_event(self.bus_event.name)
        self.bus_event_consumer._on_event(self.bus_event)

        self.acl_check.matches_required_acl.assert_called_once_with(self.bus_event.acl)
        assert_that(self.bus_event_consumer._queue.empty())


def _new_bus_event(name, has_acl=True, acl=None):
    return _BusEvent(name, has_acl, acl, sentinel.msg_body)
