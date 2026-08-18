# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from uuid import uuid4

import requests
from wazo_test_helpers import until

from .helpers.base import IntegrationTest, StandaloneAssetLaunchingTestCase, use_asset
from .helpers.websocketd import WebSocketdClient


@use_asset('standalone')
class TestStandaloneWorker(IntegrationTest):
    asset_cls = StandaloneAssetLaunchingTestCase

    def _status(self, port):
        return requests.get(f'http://127.0.0.1:{port}/status', timeout=10).json()

    def _draining_status(self, port):
        try:
            status = self._status(port)
        except requests.RequestException:
            return False
        return status if status['state'] == 'draining' else False

    def _restore_worker(self):
        self.asset_cls.restart_service('websocketd')
        self.asset_cls.wait_strategy.wait(self.asset_cls)

    async def test_standalone_serves_reports_status_and_drains(self):
        self.addCleanup(self._restore_worker)

        status_port = self.asset_cls.service_port(9504, 'websocketd')
        status = self._status(status_port)
        assert status['state'] == 'ready'
        assert status['connections'] == 0

        worker_port = self.asset_cls.service_port(9502, 'websocketd')
        client = WebSocketdClient(worker_port, loop=asyncio.get_running_loop())
        token = self.auth_client.make_token()
        await client.connect_and_wait_for_init(token)

        status = self._status(status_port)
        assert status['state'] == 'ready'
        assert status['connections'] == 1

        self.asset_cls.docker_exec(
            ['sh', '-c', f'kill -TERM {status["pid"]}'], 'websocketd'
        )

        draining = until.true(
            self._draining_status, status_port, tries=20, interval=0.25
        )
        assert draining['connections'] == 1

        client.timeout = 15
        await client.wait_for_close(code=1001)
        await client.close()
        self.auth_client.revoke_token(token)


@use_asset('standalone')
class TestStandaloneBroker(IntegrationTest):
    asset_cls = StandaloneAssetLaunchingTestCase

    async def test_the_broker_reports_itself_ready(self):
        status, body = await self.asset_cls.broker_status()

        assert status == 200
        assert body['state'] == 'ready'
        assert body['name'] == 'broker'

    async def test_a_second_connection_is_served_from_the_cache(self):
        token = self.auth_client.make_token(user_uuid=uuid4())

        await self.websocketd_client.connect_and_wait_for_init(token)
        await self.websocketd_client.close()
        after_first = self._upstream_lookups(token)
        await self.websocketd_client.connect_and_wait_for_init(token)

        assert after_first > 0  # the first one did reach wazo-auth
        assert self._upstream_lookups(token) == after_first

    async def test_clients_authenticate_while_the_broker_is_down(self):
        token = self.auth_client.make_token(user_uuid=uuid4())

        async with self.asset_cls.service_stopped('broker'):
            await self.websocketd_client.connect_and_wait_for_init(token)

    def _upstream_lookups(self, token):
        requests = self.auth_client.list_requests()['requests']
        return len([r for r in requests if token in r['path']])
