# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio

import websockets

from .test_api.base import IntegrationTest, run_with_loop
from .test_api.constants import VALID_TOKEN_ID


class TestBus(IntegrationTest):

    asset = 'basic'

    def setUp(self):
        super().setUp()
        self.event = {'name': 'foo', 'acl': None}
        self.subscribe_event_name = self.event['name']

    @run_with_loop
    def test_receive_message_with_matching_event_name(self):
        yield from self._prepare()

        event = yield from self.websocketd_client.recv_msg()

        self.assertEqual(event, self.event)

    @run_with_loop
    def test_receive_message_with_matching_acl(self):
        self.event['acl'] = 'event.foo'
        yield from self._prepare()

        event = yield from self.websocketd_client.recv_msg()

        self.assertEqual(event, self.event)

    @run_with_loop
    def test_dont_receive_message_with_non_matching_event_name(self):
        self.subscribe_event_name = 'bar'
        yield from self._prepare()

        yield from self.websocketd_client.wait_for_nothing()

    @run_with_loop
    def test_dont_receive_message_with_no_acl_defined(self):
        del self.event['acl']
        yield from self._prepare()

        yield from self.websocketd_client.wait_for_nothing()

    @run_with_loop
    def test_dont_receive_message_with_non_matching_acl(self):
        self.event['acl'] = 'token.doesnt.have.this.acl'
        yield from self._prepare()

        yield from self.websocketd_client.wait_for_nothing()

    @run_with_loop
    def test_dont_receive_message_before_start(self):
        yield from self._prepare(skip_start=True)

        yield from self.websocketd_client.wait_for_nothing()

    @asyncio.coroutine
    def _prepare(self, skip_start=False):
        yield from self.bus_client.connect()
        yield from self.websocketd_client.connect_and_wait_for_init(VALID_TOKEN_ID)
        if not skip_start:
            yield from self.websocketd_client.op_start()
        yield from self.websocketd_client.op_subscribe(self.subscribe_event_name)
        self.bus_client.publish_event(self.event)


class TestBusConnectionLost(IntegrationTest):

    asset = 'basic'

    @run_with_loop
    def test_ws_connection_is_closed_when_bus_connection_is_lost(self):
        yield from self.websocketd_client.connect_and_wait_for_init(VALID_TOKEN_ID)
        yield from self.websocketd_client.op_subscribe('foo')
        self.stop_service('rabbitmq')

        yield from self.websocketd_client.wait_for_close(code=1011)


class TestRabbitMQRestart(IntegrationTest):

    asset = 'basic'

    def setUp(self):
        super().setUp()
        self.event = {'name': 'foo', 'acl': None}

    @run_with_loop
    def test_can_connect_after_rabbitmq_restart(self):
        yield from self.websocketd_client.connect_and_wait_for_init(VALID_TOKEN_ID)
        yield from self.websocketd_client.op_subscribe('foo')
        self.restart_service('rabbitmq')
        yield from self.websocketd_client.wait_for_close(code=1011)
        yield from self.websocketd_client.close()
        yield from self._try_connect()
        yield from self.websocketd_client.op_subscribe('foo')
        yield from self.websocketd_client.op_start()
        yield from self.bus_client.connect()
        self.bus_client.publish_event(self.event)

        event = yield from self.websocketd_client.recv_msg()

        self.assertEqual(event, self.event)

    @asyncio.coroutine
    def _try_connect(self):
        # might not work on the first try since rabbitmq might not be ready
        for _ in range(5):
            try:
                yield from self.websocketd_client.connect_and_wait_for_init(VALID_TOKEN_ID)
            except websockets.ConnectionClosed:
                yield from asyncio.sleep(1, loop=self.loop)
            else:
                return
