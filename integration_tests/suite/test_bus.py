# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio

from .test_api.base import IntegrationTest
from .test_api.constants import VALID_TOKEN_ID


class TestBus(IntegrationTest):

    asset = 'basic'

    def test_bind_and_receive_message_on_xivo_exchange(self):
        self.loop.run_until_complete(self._coro_test_bind_and_receive_message_on_xivo_exchange())

    @asyncio.coroutine
    def _coro_test_bind_and_receive_message_on_xivo_exchange(self):
        body = b'hello'
        yield from self.bus_client.connect()
        yield from self.bus_client.declare_xivo_exchange()
        yield from self.websocketd_client.connect_and_wait_for_init(VALID_TOKEN_ID)
        yield from self.websocketd_client.op_bind('xivo', 'foo.bar')
        yield from self.websocketd_client.op_start()
        self.bus_client.publish_on_xivo('foo.bar', body)

        received_data = yield from self.websocketd_client.recv()

        self.assertEqual(received_data, body.decode('utf-8'))
