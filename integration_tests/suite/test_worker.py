# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from textwrap import dedent

import requests
from wazo_test_helpers import until

from .helpers.base import IntegrationTest, use_asset
from .helpers.websocketd import WebSocketdClient

_WORKER_CONFIG = '/tmp/worker.yml'
_WORKER_PIDFILE = '/tmp/worker.pid'
# the worker rewrites its process title, so it cannot be matched on its cmdline
_START_WORKER = (
    f'wazo-websocketd-worker -c {_WORKER_CONFIG} >/tmp/worker.out 2>&1 & '
    f'echo $! > {_WORKER_PIDFILE}'
)
_KILL_WORKERS = f'kill -9 $(cat {_WORKER_PIDFILE}) 2>/dev/null || true'


@use_asset('base')
class TestStandaloneWorker(IntegrationTest):
    async def test_standalone_worker_serves_reports_status_and_drains(self):
        self.addCleanup(self.asset_cls.docker_exec, ['sh', '-c', _KILL_WORKERS])
        self.asset_cls.make_filesystem().create_file(
            _WORKER_CONFIG,
            content=dedent(
                '''
                websocket:
                  listen: 0.0.0.0
                  port: 9505
                '''
            ),
        )
        self.asset_cls.docker_exec(['sh', '-c', _START_WORKER])

        status_port = self.asset_cls.service_port(9504, 'websocketd')
        status = until.true(self._ready_status, status_port, tries=20, interval=0.5)
        assert status['state'] == 'ready'
        assert status['connections'] == 0

        worker_port = self.asset_cls.service_port(9505, 'websocketd')
        client = WebSocketdClient(worker_port, loop=asyncio.get_running_loop())
        token = self.auth_client.make_token()
        await client.connect_and_wait_for_init(token)

        status = self._status(status_port)
        assert status['state'] == 'ready'
        assert status['connections'] == 1

        self.asset_cls.docker_exec(['sh', '-c', f'kill {status["pid"]}'])

        draining = until.true(
            self._draining_status, status_port, tries=20, interval=0.25
        )
        assert draining['connections'] == 1

        client.timeout = 15
        await client.wait_for_close(code=1001)
        await client.close()
        self.auth_client.revoke_token(token)

    def _status(self, port):
        return requests.get(f'http://127.0.0.1:{port}/status', timeout=10).json()

    def _ready_status(self, port):
        try:
            status = self._status(port)
        except requests.RequestException:
            return False
        return status if status['state'] == 'ready' else False

    def _draining_status(self, port):
        try:
            status = self._status(port)
        except requests.RequestException:
            return False
        return status if status['state'] == 'draining' else False
