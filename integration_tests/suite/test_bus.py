# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio

from .test_api.base import IntegrationTest
from .test_api.constants import VALID_TOKEN_ID
from .test_api.websocketd import WebSocketdTimeoutError


class TestBus(IntegrationTest):

    asset = 'basic'

    def test_receive_message_with_matching_routing_key(self):
        self.loop.run_until_complete(self._coro_test_receive_message_with_matching_routing_key())

    @asyncio.coroutine
    def _coro_test_receive_message_with_matching_routing_key(self):
        body = 'hello'
        yield from self.bus_client.connect()
        yield from self.bus_client.declare_xivo_exchange()
        yield from self.websocketd_client.connect_and_wait_for_init(VALID_TOKEN_ID)
        yield from self.websocketd_client.op_bind('xivo', 'foo.bar')
        yield from self.websocketd_client.op_start()
        self.bus_client.publish_on_xivo('foo.bar', body)

        data = yield from self.websocketd_client.recv()

        self.assertEqual(data, body)

    def test_dont_receive_message_with_non_matching_routing_key(self):
        self.loop.run_until_complete(self._coro_test_dont_receive_message_with_non_matching_routing_key())

    @asyncio.coroutine
    def _coro_test_dont_receive_message_with_non_matching_routing_key(self):
        yield from self.bus_client.connect()
        yield from self.bus_client.declare_xivo_exchange()
        yield from self.websocketd_client.connect_and_wait_for_init(VALID_TOKEN_ID)
        yield from self.websocketd_client.op_bind('xivo', 'foo.bar')
        yield from self.websocketd_client.op_start()
        self.bus_client.publish_on_xivo('foo.nomatch', 'hello')

        try:
            data = yield from self.websocketd_client.recv()
        except WebSocketdTimeoutError:
            pass
        else:
            raise AssertionError('got unexpected data from websocket: {!r}'.format(data))

    def test_receive_message_on_another_configured_exchange(self):
        self.loop.run_until_complete(self._coro_test_receive_message_on_another_configured_exchange())

    @asyncio.coroutine
    def _coro_test_receive_message_on_another_configured_exchange(self):
        body = 'hello'
        exchange_name = 'potato'
        routing_key = 'foo.bar'

        yield from self.bus_client.connect()
        yield from self.bus_client.declare_exchange(exchange_name, 'direct')
        yield from self.websocketd_client.connect_and_wait_for_init(VALID_TOKEN_ID)
        yield from self.websocketd_client.op_bind(exchange_name, routing_key)
        yield from self.websocketd_client.op_start()
        self.bus_client.publish(exchange_name, routing_key, body)

        data = yield from self.websocketd_client.recv()

        self.assertEqual(data, body)
