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
    def test_receive_message_with_matching_event_name(self):
        yield from self._prepare()

        event = yield from self.websocketd_client.recv_msg()

        self.assertEqual(event, self.event)

    @run_with_loop
    def test_receive_message_with_matching_acl(self):
        self.event['required_acl'] = 'event.foo'
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
        del self.event['required_acl']
        yield from self._prepare()

        yield from self.websocketd_client.wait_for_nothing()

    @run_with_loop
    def test_dont_receive_message_with_non_matching_acl(self):
        self.event['required_acl'] = 'token.doesnt.have.this.acl'
        yield from self._prepare()

        yield from self.websocketd_client.wait_for_nothing()

    @run_with_loop
    def test_dont_receive_message_before_start(self):
        yield from self._prepare(skip_start=True)

        yield from self.websocketd_client.wait_for_nothing()

    @asyncio.coroutine
    def _prepare(self, skip_start=False):
        yield from self.auth_server.put_token('my-token-id', acls=['websocketd', 'event.foo'])
        yield from self.bus_client.connect()
        yield from self.websocketd_client.connect_and_wait_for_init('my-token-id')
        if not skip_start:
            yield from self.websocketd_client.op_start()
        yield from self.websocketd_client.op_subscribe(self.subscribe_event_name)
        yield from asyncio.sleep(1, loop=self.loop)
        self.bus_client.publish_event(self.event)


class TestAdminSubscribe(IntegrationTest):

    asset = 'basic'

    def setUp(self):
        super().setUp()
        self.event = {
            'name': 'foo',
            'required_acl': 'event.foo',
            'tenant_uuid': 'my-token-tenant-uuid',
        }
        self.subscribe_event_name = self.event['name']
        self.subscribe_tenant_uuid = self.event['tenant_uuid']

    @run_with_loop
    def test_receive_message_with_matching_tenant_uuid(self):
        yield from self._prepare()

        event = yield from self.websocketd_client.recv_msg()

        self.assertEqual(event, self.event)

    @run_with_loop
    def test_receive_message_with_no_tenant_uuid(self):
        self.event['tenant_uuid'] = None
        yield from self._prepare()

        event = yield from self.websocketd_client.recv_msg()

        self.assertEqual(event, self.event)

    @run_with_loop
    def test_dont_receive_message_with_non_matching_tenant_uuid(self):
        self.subscribe_tenant_uuid = 'bar'
        yield from self._prepare()

        yield from self.websocketd_client.wait_for_nothing()

    @asyncio.coroutine
    def _prepare(self, skip_start=False):
        yield from self.auth_server.put_token(
            'my-token-id', tenant_uuid='my-token-tenant-uuid', acls=['websocketd', 'event.foo'],
        )
        yield from self.bus_client.connect()
        yield from self.websocketd_client.connect_and_wait_for_init('my-token-id')
        if not skip_start:
            yield from self.websocketd_client.op_start()
        yield from self.websocketd_client.op_admin_subscribe(
            self.subscribe_event_name, self.subscribe_tenant_uuid
        )
        yield from asyncio.sleep(1, loop=self.loop)
        self.bus_client.publish_event(self.event)


class TestUserSubscribe(IntegrationTest):

    asset = 'basic'

    def setUp(self):
        super().setUp()
        self.event = {
            'name': 'foo',
            'required_acl': 'event.foo',
            'tenant_uuid': 'my-token-tenant-uuid',
        }
        self.subscribe_event_name = self.event['name']

    @run_with_loop
    def test_receive_message_with_matching_tenant_uuid(self):
        yield from self._prepare()

        event = yield from self.websocketd_client.recv_msg()

        self.assertEqual(event, self.event)

    @run_with_loop
    def test_dont_receive_message_with_non_matching_tenant_uuid(self):
        self.event['tenant_uuid'] = 'bar'
        yield from self._prepare()

        yield from self.websocketd_client.wait_for_nothing()

    @run_with_loop
    def test_dont_receive_message_with_no_tenant_uuid(self):
        self.event['tenant_uuid'] = None
        yield from self._prepare()

        yield from self.websocketd_client.wait_for_nothing()

    @asyncio.coroutine
    def _prepare(self, skip_start=False):
        yield from self.auth_server.put_token(
            'my-token-id', tenant_uuid='my-token-tenant-uuid', acls=['websocketd', 'event.foo'],
        )
        yield from self.bus_client.connect()
        yield from self.websocketd_client.connect_and_wait_for_init('my-token-id')
        if not skip_start:
            yield from self.websocketd_client.op_start()
        yield from self.websocketd_client.op_user_subscribe(self.subscribe_event_name)
        yield from asyncio.sleep(1, loop=self.loop)
        self.bus_client.publish_event(self.event)


class TestBusConnectionLost(IntegrationTest):

    asset = 'basic'

    @run_with_loop
    def test_ws_connection_is_closed_when_bus_connection_is_lost(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)
        yield from self.websocketd_client.op_subscribe('foo')
        self.stop_service('rabbitmq')

        yield from self.websocketd_client.wait_for_close(code=1011)


class TestRabbitMQRestart(IntegrationTest):

    asset = 'basic'

    def setUp(self):
        super().setUp()
        self.event = {'name': 'foo', 'required_acl': None}

    @run_with_loop
    def test_can_connect_after_rabbitmq_restart(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)
        yield from self.websocketd_client.op_subscribe('foo')
        self.restart_service('rabbitmq')
        yield from self.websocketd_client.wait_for_close(code=1011)
        yield from self.websocketd_client.close()
        yield from self._try_connect()
        yield from self.websocketd_client.op_subscribe('foo')
        yield from self.websocketd_client.op_start()
        self.bus_client = self.new_bus_client()
        yield from self.bus_client.connect()
        self.bus_client.publish_event(self.event)

        event = yield from self.websocketd_client.recv_msg()

        self.assertEqual(event, self.event)

    @asyncio.coroutine
    def _try_connect(self):
        # might not work on the first try since rabbitmq might not be ready
        for _ in range(10):
            try:
                yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)
            except websockets.ConnectionClosed:
                yield from asyncio.sleep(1, loop=self.loop)
            else:
                return
