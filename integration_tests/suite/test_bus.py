# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio

from contextlib import asynccontextmanager
from uuid import uuid4

from .helpers.constants import (
    MASTER_TENANT_UUID,
    TENANT1_UUID,
    TENANT2_UUID,
    USER1_UUID,
    USER2_UUID,
)
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
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event):
            await self.bus_client.publish(event, user_uuid=USER1_UUID)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, user_uuid='*')
            self.assertEqual(event, await self.websocketd_client.recv_msg())

    @run_with_loop
    async def test_user_dont_receive_event_for_other_users(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event):
            await self.bus_client.publish(event, user_uuid=USER2_UUID)
            await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_user_dont_receive_events_from_other_tenants(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event):
            await self.bus_client.publish(event, tenant_uuid=TENANT2_UUID)
            await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_user_dont_receive_events_from_master_tenant(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event):
            await self.bus_client.publish(event, tenant_uuid=MASTER_TENANT_UUID)
            await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_user_receives_all_tenant_events_when_admin(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, purpose='user', admin=True):
            await self.bus_client.publish(event, user_uuid=USER1_UUID)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, user_uuid='*')
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, user_uuid=USER2_UUID)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

    @run_with_loop
    async def test_user_dont_receive_events_from_other_tenants_when_admin(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, purpose='user', admin=True):
            await self.bus_client.publish(event, tenant_uuid=TENANT2_UUID)
            await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_master_tenant_user_receives_all_messages(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, tenant_uuid=MASTER_TENANT_UUID):
            await self.bus_client.publish(
                event, tenant_uuid=TENANT1_UUID, user_uuid=USER1_UUID
            )
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(
                event, tenant_uuid=TENANT2_UUID, user_uuid=USER2_UUID
            )
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, tenant_uuid=MASTER_TENANT_UUID)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

    @run_with_loop
    async def test_external_api_receives_all_tenant_events(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, purpose='external_api'):
            await self.bus_client.publish(event, user_uuid=USER1_UUID)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, user_uuid='*')
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, user_uuid=USER2_UUID)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

    @run_with_loop
    async def test_external_api_dont_receive_events_from_other_tenants(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, purpose='external_api'):
            await self.bus_client.publish(event, tenant_uuid=TENANT2_UUID)
            await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_internal_user_receives_all_tenant_events(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, purpose='internal'):
            await self.bus_client.publish(event, user_uuid=USER1_UUID)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, user_uuid='*')
            self.assertEqual(event, await self.websocketd_client.recv_msg())

            await self.bus_client.publish(event, user_uuid=USER2_UUID)
            self.assertEqual(event, await self.websocketd_client.recv_msg())

    @run_with_loop
    async def test_internal_user_dont_receive_events_from_other_tenants(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, purpose='internal'):
            await self.bus_client.publish(event, tenant_uuid=TENANT2_UUID)
            await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_user_dont_receive_message_from_other_origin_uuid(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event):
            await self.bus_client.publish(event, origin_uuid='some-other-wazo-uuid')
            await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_admin_dont_receive_message_from_other_origin_uuid(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, admin=True):
            await self.bus_client.publish(event, origin_uuid='some-other-wazo-uuid')
            await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_internal_user_dont_receive_message_from_other_origin_uuid(self):
        event = {'name': 'foo', 'required_acl': 'event.foo'}

        async with self._connect(event, purpose='internal'):
            await self.bus_client.publish(event, origin_uuid='some-other-wazo-uuid')
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
        user_uuid=USER1_UUID,
        tenant_uuid=TENANT1_UUID,
        purpose='user',
        admin=False,
    ):
        token = self.auth_client.make_token(
            user_uuid=user_uuid,
            tenant_uuid=tenant_uuid,
            purpose=purpose,
            acl=['websocketd', event['required_acl']],
            admin=admin,
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
            await self.websocketd_client.close()


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

        with self.auth_client.token() as token:
            await self.websocketd_client.connect_and_wait_for_init(token)
            await self.websocketd_client.op_subscribe('foo')
            self.restart_service('rabbitmq')
            await self.websocketd_client.wait_for_close(code=1011)
            await self.websocketd_client.close()
            self.bus_client = self.make_bus()
            await self.bus_client.connect()
            await self.websocketd_client.retry_connect_and_wait_for_init(token)
            await self.websocketd_client.op_subscribe('foo')
            await self.websocketd_client.op_start()
            await self.bus_client.publish(event)

            received_event = await self.websocketd_client.recv_msg()
        self.assertEqual(event, received_event)


class TestClientPing(IntegrationTest):
    asset = 'basic'

    @run_with_loop
    async def test_receive_pong_on_client_ping(self):
        with self.auth_client.token() as token:
            await self.websocketd_client.connect_and_wait_for_init(token, version=2)
            await self.websocketd_client.op_ping()
