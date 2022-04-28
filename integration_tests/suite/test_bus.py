# Copyright 2016-2022 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio

from .helpers.constants import MASTER_TENANT_UUID, TENANT1_UUID, TENANT2_UUID
from .helpers.base import IntegrationTest, run_with_loop


class TestBus(IntegrationTest):

    asset = 'basic'

    def setUp(self):
        super().setUp()
        self.event = {'name': 'foo', 'required_acl': None}
        self.subscribe_event_name = self.event['name']
        self.tenant_uuid = TENANT1_UUID
        self.token = self.auth_client.make_token(acl=['websocketd', 'event.foo'])

    def tearDown(self):
        self.auth_client.revoke_token(self.token)
        super().tearDown()

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
        self.assertEqual({"op": "event", "code": 0, "data": self.event}, event)

        with self.auth_client.token(acl=['websocketd']) as token:
            await self.websocketd_client.op_token(token)
            await self.bus_client.publish(self.event, self.tenant_uuid)
            await self.websocketd_client.wait_for_nothing()

        # Got right again
        with self.auth_client.token(acl=['websocketd', 'event.foo']) as token:
            await self.websocketd_client.op_token(token)
            await self.bus_client.publish(self.event, self.tenant_uuid)
            event = await self.websocketd_client.recv_msg()
        self.assertEqual({"op": "event", "code": 0, "data": self.event}, event)

    @run_with_loop
    async def test_dont_receive_message_from_other_tenant(self):
        self.event = {'name': 'foo', 'required_acl': 'event.foo'}
        await self._prepare(skip_publish=True)
        await self.bus_client.publish(self.event, tenant_uuid=TENANT2_UUID)
        await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_dont_receive_message_from_master_tenant_when_user(self):
        self.event = {'name': 'foo', 'required_acl': 'event.foo'}
        await self._prepare(skip_publish=True)
        await self.bus_client.publish(self.event, tenant_uuid=MASTER_TENANT_UUID)
        await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_receive_all_messages_when_master_tenant(self):
        self.event = {'name': 'foo', 'required_acl': 'event.foo'}
        self.tenant_uuid = MASTER_TENANT_UUID
        with self.auth_client.token(
            tenant_uuid=MASTER_TENANT_UUID, acl=['websocketd', 'event.foo']
        ) as token:
            await self.bus_client.connect()
            await self.websocketd_client.connect_and_wait_for_init(token)
            await self.websocketd_client.op_start()
            await self.websocketd_client.op_subscribe('foo')
            await asyncio.sleep(1)
            await self.bus_client.publish(self.event, tenant_uuid=TENANT1_UUID)
            events = await self.websocketd_client.recv_msg()
        self.assertEqual(events, self.event)

    async def _prepare(self, skip_start=False, version=1, skip_publish=False):
        await self.bus_client.connect()
        await self.websocketd_client.connect_and_wait_for_init(
            self.token, version=version
        )
        if not skip_start:
            await self.websocketd_client.op_start()
        await self.websocketd_client.op_subscribe(self.subscribe_event_name)
        await asyncio.sleep(1)
        if not skip_publish:
            await self.bus_client.publish(self.event, self.tenant_uuid)


class TestBusConnectionLost(IntegrationTest):

    asset = 'basic'

    @run_with_loop
    async def test_ws_connection_is_closed_when_bus_connection_is_lost(self):
        with self.auth_client.token() as token:
            await self.websocketd_client.connect_and_wait_for_init(token)
            await self.websocketd_client.op_subscribe('foo')
            self.stop_service('rabbitmq')

            await self.websocketd_client.wait_for_close(code=1011)


class TestRabbitMQRestart(IntegrationTest):

    asset = 'basic'

    @run_with_loop
    async def test_can_connect_after_rabbitmq_restart(self):
        event = {'name': 'foo', 'required_acl': None}
        tenant_uuid = TENANT1_UUID

        with self.auth_client.token(tenant_uuid=tenant_uuid) as token:
            await self.websocketd_client.connect_and_wait_for_init(token)
            await self.websocketd_client.op_subscribe('foo')
            self.restart_service('rabbitmq')
            await self.websocketd_client.wait_for_close(code=1011)
            await self.websocketd_client.close()
            self.bus_client = self.make_bus()
            await self.bus_client.connect()
            await self.wait_strategy.await_for_connection(self)
            await self.websocketd_client.connect_and_wait_for_init(token)
            await self.websocketd_client.op_subscribe('foo')
            await self.websocketd_client.op_start()
            await self.bus_client.publish(event, tenant_uuid)

            received_event = await self.websocketd_client.recv_msg()
        self.assertEqual(event, received_event)


class TestClientPing(IntegrationTest):

    asset = 'basic'

    @run_with_loop
    async def test_receive_pong_on_client_ping(self):
        with self.auth_client.token() as token:
            await self.websocketd_client.connect_and_wait_for_init(token, version=2)
            await self.websocketd_client.op_ping()
