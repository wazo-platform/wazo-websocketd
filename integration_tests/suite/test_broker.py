# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from uuid import uuid4

from .helpers.base import IntegrationTest, use_asset


@use_asset('base')
class TestBroker(IntegrationTest):
    async def test_the_broker_reports_itself_ready(self):
        status, body = await self.asset_cls.broker_status()

        self.assertEqual(status, 200)
        self.assertEqual(body['state'], 'ready')
        self.assertEqual(body['name'], 'broker')

    async def test_a_second_connection_is_served_from_the_cache(self):
        token = self.auth_client.make_token(user_uuid=uuid4())

        await self.websocketd_client.connect_and_wait_for_init(token)
        await self.websocketd_client.close()
        after_first = self._upstream_lookups(token)
        await self.websocketd_client.connect_and_wait_for_init(token)

        self.assertGreater(after_first, 0)  # the first one did reach wazo-auth
        self.assertEqual(self._upstream_lookups(token), after_first)

    async def test_clients_authenticate_while_the_broker_is_down(self):
        token = self.auth_client.make_token(user_uuid=uuid4())

        async with self.asset_cls.service_stopped('broker'):
            await self.websocketd_client.connect_and_wait_for_init(token)

    def _upstream_lookups(self, token):
        requests = self.auth_client.list_requests()['requests']
        return len([r for r in requests if token in r['path']])
