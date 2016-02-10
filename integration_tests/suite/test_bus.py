# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio

from .test_api.base import IntegrationTest, run_with_loop
from .test_api.constants import VALID_TOKEN_ID
from .test_api.websocketd import WebSocketdTimeoutError


class TestBus(IntegrationTest):

    asset = 'basic'

    def setUp(self):
        super().setUp()
        self.exchange_name = 'xivo'
        self.exchange_type = 'topic'
        self.exchange_durable = True
        self.binding_key = 'foo'
        self.routing_key = self.binding_key
        self.body = 'hello'

    @run_with_loop
    def test_receive_message_with_matching_routing_key(self):
        yield from self._prepare()

        data = yield from self.websocketd_client.recv()

        self.assertEqual(data, self.body)

    @run_with_loop
    def test_dont_receive_message_with_non_matching_routing_key(self):
        self.routing_key = 'bar'
        yield from self._prepare()

        try:
            data = yield from self.websocketd_client.recv()
        except WebSocketdTimeoutError:
            pass
        else:
            raise AssertionError('got unexpected data from websocket: {!r}'.format(data))

    @run_with_loop
    def test_receive_message_on_another_configured_exchange(self):
        self.exchange_name = 'potato'
        self.exchange_type = 'direct'
        self.exchange_durable = False
        yield from self._prepare()

        data = yield from self.websocketd_client.recv()

        self.assertEqual(data, self.body)

    @asyncio.coroutine
    def _prepare(self):
        yield from self.bus_client.connect()
        yield from self.bus_client.declare_exchange(self.exchange_name, self.exchange_type, self.exchange_durable)
        yield from self.websocketd_client.connect_and_wait_for_init(VALID_TOKEN_ID)
        yield from self.websocketd_client.op_bind(self.exchange_name, self.binding_key)
        yield from self.websocketd_client.op_start()
        self.bus_client.publish(self.exchange_name, self.routing_key, self.body)