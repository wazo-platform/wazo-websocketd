# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio

from wazo_test_helpers import until

from .helpers.base import IntegrationTest, use_asset


@use_asset('base')
class TestSupervisor(IntegrationTest):
    async def test_a_killed_worker_is_replaced(self):
        workers = self._workers()
        assert len(workers) == 2

        victim, pid = sorted(workers.items())[0]
        self.asset_cls.docker_exec(['sh', '-c', f'kill -9 {pid}'])

        replacements = until.true(self._pool_healed, victim, tries=20, interval=1)
        assert len(replacements) == 2

        with self.auth_client.token() as token:
            await self.websocketd_client.connect_and_wait_for_init(token)

    async def test_sigttin_and_sigttou_resize_the_pool(self):
        assert len(self._workers()) == 2

        self.asset_cls.docker_exec(['sh', '-c', 'kill -TTIN 1'])
        until.true(self._worker_count_is, 3, tries=20, interval=1)

        self.asset_cls.docker_exec(['sh', '-c', 'kill -TTOU 1'])
        until.true(self._worker_count_is, 2, tries=30, interval=1)

    async def test_sigusr2_reexec_preserves_children_and_connections(self):
        with self.auth_client.token() as token:
            await self.websocketd_client.connect_and_wait_for_init(token, version=2)
            await self.websocketd_client.op_start()
            before = self._workers()

            self.asset_cls.docker_exec(['sh', '-c', 'kill -USR2 1'])
            await asyncio.sleep(5)

            assert self._workers() == before
            assert self._broker_pid() is not None
            await self.websocketd_client.op_ping()

    def _statuses(self):
        return {
            worker_id: status['pid']
            for worker_id, status in self.asset_cls.read_children_statuses().items()
            if status['state'] != 'draining'
        }

    def _workers(self):
        return {
            name: pid
            for name, pid in self._statuses().items()
            if name.startswith('worker-')
        }

    def _broker_pid(self):
        return self._statuses().get('broker')

    def _worker_count_is(self, expected):
        return len(self._workers()) == expected

    def _pool_healed(self, victim):
        workers = self._workers()
        if len(workers) != 2 or victim in workers:
            return False
        return workers
