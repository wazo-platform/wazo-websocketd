# Copyright 2016-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio

import websockets

from .helpers.base import IntegrationTest, run_with_loop


class TestBus(IntegrationTest):

    asset = 'basic'

    def setUp(self):
        super().setUp()
        self.event = {'name': 'foo', 'required_acl': None}
        self.subscribe_event_name = self.event['name']

    @run_with_loop
    async def test_receive_message_with_matching_event_name(self):
        await self._prepare()

        event = await self.websocketd_client.recv_msg()

        self.assertEqual(event, self.event)

    @run_with_loop
    async def test_receive_message_with_matching_acl(self):
        self.event['required_acl'] = 'event.foo'
        await self._prepare()

        event = await self.websocketd_client.recv_msg()

        self.assertEqual(event, self.event)

    @run_with_loop
    async def test_dont_receive_message_with_non_matching_event_name(self):
        self.subscribe_event_name = 'bar'
        await self._prepare()

        await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_dont_receive_message_with_no_acl_defined(self):
        del self.event['required_acl']
        await self._prepare()

        await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_dont_receive_message_with_non_matching_acl(self):
        self.event['required_acl'] = 'token.doesnt.have.this.acl'
        await self._prepare()

        await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_dont_receive_message_before_start(self):
        await self._prepare(skip_start=True)

        await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_receive_message_v2(self):
        self.event = {'name': 'foo', 'required_acl': 'event.foo'}
        await self._prepare(version=2)
        event = await self.websocketd_client.recv_msg()
        self.assertEqual({"op": "event", "code": 0, "msg": self.event}, event)

        self.auth_server.put_token('useless-token', acls=['websocketd'])
        await self.websocketd_client.op_token("useless-token")
        self.bus_client.publish_event(self.event)
        await self.websocketd_client.wait_for_nothing()

        # Got right again
        self.auth_server.put_token('my-new-token-id', acls=['websocketd', 'event.foo'])
        await self.websocketd_client.op_token("my-new-token-id")
        self.bus_client.publish_event(self.event)
        event = await self.websocketd_client.recv_msg()
        self.assertEqual({"op": "event", "code": 0, "msg": self.event}, event)

    async def _prepare(self, skip_start=False, version=1):
        self.auth_server.put_token('my-token-id', acls=['websocketd', 'event.foo'])
        await self.bus_client.connect()
        await self.websocketd_client.connect_and_wait_for_init(
            'my-token-id', version=version
        )
        if not skip_start:
            await self.websocketd_client.op_start()
        await self.websocketd_client.op_subscribe(self.subscribe_event_name)
        await asyncio.sleep(1, loop=self.loop)
        self.bus_client.publish_event(self.event)


class TestBusConnectionLost(IntegrationTest):

    asset = 'basic'

    @run_with_loop
    async def test_ws_connection_is_closed_when_bus_connection_is_lost(self):
        await self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)
        await self.websocketd_client.op_subscribe('foo')
        self.stop_service('rabbitmq')

        await self.websocketd_client.wait_for_close(code=1011)


class TestRabbitMQRestart(IntegrationTest):

    asset = 'basic'

    def setUp(self):
        super().setUp()
        self.event = {'name': 'foo', 'required_acl': None}

    @run_with_loop
    async def test_can_connect_after_rabbitmq_restart(self):
        await self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)
        await self.websocketd_client.op_subscribe('foo')
        self.restart_service('rabbitmq')
        await self.websocketd_client.wait_for_close(code=1011)
        await self.websocketd_client.close()
        await self._try_connect()
        await self.websocketd_client.op_subscribe('foo')
        await self.websocketd_client.op_start()
        self.bus_client = self.new_bus_client()
        await self.bus_client.connect()
        self.bus_client.publish_event(self.event)

        event = await self.websocketd_client.recv_msg()

        self.assertEqual(event, self.event)

    async def _try_connect(self):
        # might not work on the first try since rabbitmq might not be ready
        for _ in range(10):
            try:
                await self.websocketd_client.connect_and_wait_for_init(
                    self.valid_token_id
                )
            except websockets.ConnectionClosed:
                await asyncio.sleep(1, loop=self.loop)
            else:
                return
