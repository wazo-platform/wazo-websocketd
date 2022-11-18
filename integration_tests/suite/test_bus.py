# Copyright 2016-2022 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio

from contextlib import asynccontextmanager
from uuid import uuid4

from .helpers.constants import MASTER_TENANT_UUID, TENANT1_UUID, TENANT2_UUID
from .helpers.base import IntegrationTest, run_with_loop


class TestBus(IntegrationTest):

    asset = 'basic'

    def setUp(self):
        super().setUp()
        self.event = {'name': 'foo', 'required_acl': None}
        self.subscribe_event_name = self.event['name']
        self.tenant_uuid = TENANT1_UUID
        self.user_uuid = uuid4()
        self.token = self.auth_client.make_token(
            user_uuid=self.user_uuid, acl=['websocketd', 'event.foo']
        )

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
            await self.bus_client.publish(self.event, self.tenant_uuid, self.user_uuid)
            await self.websocketd_client.wait_for_nothing()

        # Got right again
        with self.auth_client.token(acl=['websocketd', 'event.foo']) as token:
            await self.websocketd_client.op_token(token)
            await self.bus_client.publish(self.event, self.tenant_uuid, self.user_uuid)
            event = await self.websocketd_client.recv_msg()
        self.assertEqual({"op": "event", "code": 0, "data": self.event}, event)

    @run_with_loop
    async def test_user_receives_user_events(self):
        uuid = uuid4()
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, user_uuid=uuid):
            await self.bus_client.publish(event, user_uuid=uuid)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, user_uuid='*')
            self.assertEqual(event, await self.websocketd_client.recv_msg())

    @run_with_loop
    async def test_user_dont_receive_event_for_other_users(self):
        uuid = uuid4()
        other_uuid = uuid4()
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, user_uuid=uuid, tenant_uuid=TENANT1_UUID):
            await self.bus_client.publish(
                event, user_uuid=other_uuid, tenant_uuid=TENANT1_UUID
            )
            await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_user_dont_receive_events_from_other_tenants(self):
        uuid = uuid4()
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, user_uuid=uuid, tenant_uuid=TENANT1_UUID):
            await self.bus_client.publish(
                event, user_uuid=uuid, tenant_uuid=TENANT2_UUID
            )
            await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_user_dont_receive_events_from_master_tenant(self):
        uuid = uuid4()
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, user_uuid=uuid, tenant_uuid=TENANT1_UUID):
            await self.bus_client.publish(
                event, user_uuid=uuid, tenant_uuid=MASTER_TENANT_UUID
            )
            await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_master_tenant_user_receives_all_messages(self):
        uuid = uuid4()
        other_uuid = uuid4()
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, tenant_uuid=MASTER_TENANT_UUID):
            await self.bus_client.publish(
                event, tenant_uuid=TENANT1_UUID, user_uuid=uuid
            )
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(
                event, tenant_uuid=TENANT2_UUID, user_uuid=other_uuid
            )
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, tenant_uuid=MASTER_TENANT_UUID)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

    @run_with_loop
    async def test_external_api_receives_all_tenant_events(self):
        uuid = uuid4()
        other_uuid = uuid4()
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, user_uuid=uuid, purpose='external_api'):
            await self.bus_client.publish(event, user_uuid=uuid)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, user_uuid='*')
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, user_uuid=other_uuid)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

    @run_with_loop
    async def test_external_api_dont_receive_events_from_other_tenants(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(
            event, tenant_uuid=TENANT1_UUID, purpose='external_api'
        ):
            await self.bus_client.publish(event, tenant_uuid=TENANT2_UUID)
            await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_internal_user_receives_all_tenant_events(self):
        uuid = uuid4()
        other_uuid = uuid4()
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, user_uuid=uuid, purpose='internal'):
            await self.bus_client.publish(event, user_uuid=uuid)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, user_uuid='*')
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, user_uuid=other_uuid)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

    @run_with_loop
    async def test_internal_user_dont_receive_events_from_other_tenants(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, tenant_uuid=TENANT1_UUID, purpose='internal'):
            await self.bus_client.publish(event, tenant_uuid=TENANT2_UUID)
            await self.websocketd_client.wait_for_nothing()

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
            await self.bus_client.publish(
                self.event, user_uuid=self.user_uuid, tenant_uuid=self.tenant_uuid
            )

    @asynccontextmanager
    async def _connect(
        self,
        event,
        *,
        version=1,
        user_uuid=None,
        tenant_uuid=TENANT1_UUID,
        purpose='user',
    ):
        token = self.auth_client.make_token(
            user_uuid=user_uuid,
            tenant_uuid=tenant_uuid,
            purpose=purpose,
            acl=['websocketd', event['required_acl']],
        )
        await self.bus_client.connect()
        await self.websocketd_client.connect_and_wait_for_init(token, version=version)
        await self.websocketd_client.op_start()
        await self.websocketd_client.op_subscribe(event['name'])
        await asyncio.sleep(1)
        try:
            yield
        finally:
            self.auth_client.revoke_token(token)


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
        user_uuid = uuid4()
        tenant_uuid = TENANT1_UUID

        with self.auth_client.token(
            user_uuid=user_uuid, tenant_uuid=tenant_uuid
        ) as token:
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
            await self.bus_client.publish(event, tenant_uuid, user_uuid)

            received_event = await self.websocketd_client.recv_msg()
        self.assertEqual(event, received_event)


class TestClientPing(IntegrationTest):

    asset = 'basic'

    @run_with_loop
    async def test_receive_pong_on_client_ping(self):
        with self.auth_client.token() as token:
            await self.websocketd_client.connect_and_wait_for_init(token, version=2)
            await self.websocketd_client.op_ping()
