# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from signal import SIGKILL, SIGTERM

from ..registry import WorkerRegistry
from ..status import WorkerState
from .helpers import Clock, spawn_sleeper


class TestRegistry:
    def test_added_worker_is_listed_and_looked_up(self):
        registry = WorkerRegistry()

        entry = registry.add('worker-1', '/run/x/worker-1.sock')

        assert registry.get('worker-1') is entry
        assert registry.workers() == [entry]
        assert entry.socket_path == '/run/x/worker-1.sock'

    def test_remove_returns_the_entry_and_forgets_it(self):
        registry = WorkerRegistry()
        entry = registry.add('worker-1', '/run/x/worker-1.sock')

        assert registry.remove('worker-1') is entry
        assert registry.get('worker-1') is None
        assert registry.remove('worker-1') is None

    def test_record_status_updates_gauges_and_freshness(self):
        clock = Clock()
        registry = WorkerRegistry(timer=clock)
        entry = registry.add('worker-1', '/run/x/worker-1.sock')

        clock.now = 1010.0
        registry.record_status('worker-1', pid=42, connections=7, state='ready')

        assert entry.pid == 42
        assert entry.connections == 7
        assert entry.draining is False
        assert entry.is_ready is True
        assert entry.last_seen == 1010.0

    def test_a_reported_state_does_not_clear_a_drain_we_asked_for(self):
        registry = WorkerRegistry()
        entry = registry.add('worker-1', '/run/x/worker-1.sock')
        entry.state = WorkerState.DRAINING

        # the worker keeps answering `ready` until it processes our SIGTERM
        registry.record_status('worker-1', pid=42, connections=3, state='ready')

        assert entry.draining is True
        assert entry.connections == 3  # the rest of the report still lands

    def test_connection_totals_ignore_draining_workers(self):
        registry = WorkerRegistry()
        registry.add('worker-1', '/run/x/worker-1.sock')
        registry.add('worker-2', '/run/x/worker-2.sock')
        registry.record_status('worker-1', pid=1, connections=10, state='ready')
        registry.record_status('worker-2', pid=2, connections=90, state='draining')

        assert registry.total_connections() == 100
        assert len(registry.active_workers()) == 1
        assert [entry.worker_id for entry in registry.drainable()] == ['worker-1']


class TestResponsiveness:
    def test_unresponsive_workers_are_those_not_seen_recently(self):
        clock = Clock()
        registry = WorkerRegistry(timer=clock)
        fresh = registry.add('worker-1', '/run/x/worker-1.sock')
        stale = registry.add('worker-2', '/run/x/worker-2.sock')

        clock.now = 1015.0
        registry.record_status('worker-1', pid=1, connections=0, state='ready')
        clock.now = 1020.0

        assert registry.unresponsive(max_silence=10.0) == [stale]
        assert fresh not in registry.unresponsive(max_silence=10.0)

    def test_new_workers_get_a_grace_period_from_their_add_time(self):
        clock = Clock()
        registry = WorkerRegistry(timer=clock)
        registry.add('worker-1', '/run/x/worker-1.sock')

        clock.now = 1005.0
        assert registry.unresponsive(max_silence=10.0) == []

    def test_a_worker_we_are_stopping_is_no_longer_reported_unresponsive(self):
        clock = Clock()
        registry = WorkerRegistry(timer=clock)
        entry = registry.add('worker-1', '/run/x/worker-1.sock')
        clock.now += 60

        assert registry.unresponsive(max_silence=15.0) == [entry]

        entry.stopping = True
        assert registry.unresponsive(max_silence=15.0) == []


class TestSignalling:
    def test_an_entry_holding_neither_a_process_nor_a_pid_is_not_running(self):
        entry = WorkerRegistry().add('worker-1', '/run/x/worker-1.sock')

        assert entry.is_running() is False
        entry.signal()

    async def test_an_entry_signals_the_process_it_owns(self):
        entry = WorkerRegistry().add('worker-1', '/run/x/worker-1.sock')
        entry.process = await spawn_sleeper()

        assert entry.is_running() is True
        entry.signal()

        assert await asyncio.wait_for(entry.process.wait(), timeout=5) == -SIGTERM
        assert entry.is_running() is False

    async def test_an_entry_signals_an_adopted_pid_it_does_not_own(self):
        entry = WorkerRegistry().add('worker-1', '/run/x/worker-1.sock')
        process = await spawn_sleeper()
        entry.pid = process.pid

        assert entry.is_running() is True
        entry.signal(SIGKILL)

        assert await asyncio.wait_for(process.wait(), timeout=5) == -SIGKILL

    async def test_an_owned_process_wins_over_a_stale_adopted_pid(self):
        entry = WorkerRegistry().add('worker-1', '/run/x/worker-1.sock')
        entry.process = await spawn_sleeper()
        entry.pid = entry.process.pid

        entry.signal(SIGKILL)

        assert await asyncio.wait_for(entry.process.wait(), timeout=5) == -SIGKILL
