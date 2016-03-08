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
        self.routing_key = self.binding_key
        self.body = 'hello'

    @run_with_loop
    def test_dont_receive_message_before_start(self):
        yield from self._prepare(skip_start=True)

        try:
            data = yield from self.websocketd_client.recv()
        except WebSocketdTimeoutError:
            pass
        else:
            raise AssertionError('got unexpected data from websocket: {!r}'.format(data))

    @asyncio.coroutine
    def _prepare(self, skip_start=False):
        yield from self.bus_client.connect()
        yield from self.bus_client.declare_exchange(self.exchange_name, self.exchange_type, self.exchange_durable)
        yield from self.websocketd_client.connect_and_wait_for_init(VALID_TOKEN_ID)
        if not skip_start:
            yield from self.websocketd_client.op_start()
        self.bus_client.publish(self.exchange_name, self.routing_key, self.body)
