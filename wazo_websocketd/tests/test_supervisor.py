# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import contextlib
from unittest.mock import Mock

import pytest

from ..auth import MasterTenantProxy
from ..ipc import Heartbeat, Revoke
from ..supervisor import Supervisor
from .helpers import control_pair, make_tcp_listener, run_async


class _FakeProcess:
    def __init__(self, *, exits_on_terminate=True):
        self.pid = 4242
        self._alive = True
        self._exits_on_terminate = exits_on_terminate
        self.terminated = False
        self.killed = False
        self.join_calls = 0

    def is_alive(self):
        return self._alive

    def terminate(self):
        self.terminated = True
        if self._exits_on_terminate:
            self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def join(self, timeout=None):
        self.join_calls += 1


def _spawn_factory(**process_kwargs):
    processes = {}

    def spawn(worker_sock, worker_id):
        process = _FakeProcess(**process_kwargs)
        processes[worker_id] = process
        return process

    return spawn, processes


def _supervisor(spawn, process_workers):
    listener, _ = make_tcp_listener()
    config = {'process_workers': process_workers}
    supervisor = Supervisor(
        config, listener, Mock(), MasterTenantProxy.proxy, spawn=spawn
    )
    return supervisor, listener


async def _wait_until(predicate, *, attempts=100, interval=0.005):
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(interval)


@contextlib.asynccontextmanager
async def _reader_for_worker(worker_id='w1', *, process=None):
    spawn, _ = _spawn_factory()
    supervisor, listener = _supervisor(spawn, process_workers=0)
    dispatcher_sock, worker_sock = control_pair()
    entry = supervisor.registry.add(worker_id, dispatcher_sock)
    entry.process = process
    reader = asyncio.get_event_loop().create_task(
        supervisor._read_heartbeats(worker_id, dispatcher_sock)
    )
    entry.heartbeat_task = reader
    try:
        yield supervisor, worker_sock, dispatcher_sock
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        listener.close()
        for sock in (dispatcher_sock, worker_sock):
            if sock.fileno() != -1:
                sock.close()


def test_supervisor_spawns_initial_workers():
    async def scenario():
        spawn, processes = _spawn_factory()
        supervisor, listener = _supervisor(spawn, process_workers=3)
        async with supervisor:
            registered = len(supervisor.registry.workers())
        listener.close()
        return registered, sorted(processes)

    registered, spawned = run_async(scenario())
    assert registered == 3
    assert spawned == ['worker-1', 'worker-2', 'worker-3']


def test_add_worker_registers_and_spawns():
    async def scenario():
        spawn, processes = _spawn_factory()
        supervisor, listener = _supervisor(spawn, process_workers=1)
        async with supervisor:
            worker_id = supervisor.add_worker()
            present = supervisor.registry.get(worker_id) is not None
        listener.close()
        return worker_id, present, sorted(processes)

    worker_id, present, spawned = run_async(scenario())
    assert worker_id == 'worker-2'
    assert present is True
    assert spawned == ['worker-1', 'worker-2']


def test_scale_down_drains_and_terminates_most_recent_worker():
    async def scenario():
        spawn, processes = _spawn_factory()
        supervisor, listener = _supervisor(spawn, process_workers=2)
        async with supervisor:
            supervisor.scale_down()
            draining = [
                w.worker_id for w in supervisor.registry.workers() if w.draining
            ]
            terminated = processes['worker-2'].terminated
        listener.close()
        return draining, terminated

    draining, terminated = run_async(scenario())
    assert draining == ['worker-2']
    assert terminated is True


def test_shutdown_terminates_responsive_workers_without_killing():
    async def scenario():
        spawn, processes = _spawn_factory(exits_on_terminate=True)
        supervisor, listener = _supervisor(spawn, process_workers=2)
        async with supervisor:
            pass
        listener.close()
        return list(processes.values())

    processes = run_async(scenario())
    assert all(p.terminated for p in processes)
    assert not any(p.killed for p in processes)


def test_shutdown_kills_workers_that_do_not_exit_in_time():
    async def scenario():
        spawn, processes = _spawn_factory(exits_on_terminate=False)
        supervisor, listener = _supervisor(spawn, process_workers=1)
        async with supervisor:
            pass
        listener.close()
        return processes['worker-1']

    process = run_async(scenario())
    assert process.terminated is True
    assert process.killed is True


def test_reap_removes_terminates_and_closes_a_dead_worker():
    async def scenario():
        spawn, processes = _spawn_factory(exits_on_terminate=False)
        supervisor, listener = _supervisor(spawn, process_workers=1)
        async with supervisor:
            worker_id = supervisor.registry.workers()[0].worker_id
            sock = supervisor.registry.get(worker_id).control_sock
            supervisor._reap(worker_id)
            gone = supervisor.registry.get(worker_id) is None
            sock_closed = sock.fileno() == -1
        listener.close()
        return gone, sock_closed, processes[worker_id]

    gone, sock_closed, process = run_async(scenario())
    assert gone is True
    assert sock_closed is True
    assert process.terminated is True


def test_read_heartbeats_updates_registry():
    assert run_async(_heartbeat_scenario()) == 6


async def _heartbeat_scenario():
    async with _reader_for_worker() as (supervisor, worker_sock, _):
        entry = supervisor.registry.get('w1')
        worker_sock.send(Heartbeat(6).marshal())
        await _wait_until(lambda: entry.connection_count == 6)
        return entry.connection_count


def test_reconcile_respawns_crashed_worker_to_desired_count():
    async def scenario():
        spawn, _ = _spawn_factory()
        supervisor, listener = _supervisor(spawn, process_workers=2)
        async with supervisor:
            victim = supervisor.registry.workers()[0].worker_id
            supervisor._reap(victim)
            after_crash = len(supervisor.registry.workers())
            supervisor._reconcile()
            after_reconcile = len(supervisor.registry.workers())
        listener.close()
        return after_crash, after_reconcile

    after_crash, after_reconcile = run_async(scenario())
    assert after_crash == 1
    assert after_reconcile == 2


def test_reconcile_does_not_respawn_intentionally_drained_worker():
    async def scenario():
        spawn, _ = _spawn_factory()
        supervisor, listener = _supervisor(spawn, process_workers=2)
        async with supervisor:
            supervisor.scale_down()
            drained = next(
                w.worker_id for w in supervisor.registry.workers() if w.draining
            )
            supervisor._reap(drained)
            supervisor._reconcile()
            count = len(supervisor.registry.workers())
        listener.close()
        return count

    assert run_async(scenario()) == 1


def test_read_heartbeats_reaps_worker_when_control_channel_closes():
    assert run_async(_eof_reap_scenario()) == (True, True, True)


async def _eof_reap_scenario():
    process = _FakeProcess(exits_on_terminate=False)
    async with _reader_for_worker(process=process) as (
        supervisor,
        worker_sock,
        dispatcher_sock,
    ):
        worker_sock.close()
        await _wait_until(lambda: supervisor.registry.get('w1') is None)
        return (
            supervisor.registry.get('w1') is None,
            dispatcher_sock.fileno() == -1,
            process.terminated,
        )


def test_read_control_invalidates_token_on_revoke():
    assert run_async(_revoke_scenario()) is True


async def _revoke_scenario():
    async with _reader_for_worker() as (supervisor, worker_sock, _):
        worker_sock.send(Revoke('tok-xyz').marshal())
        await _wait_until(lambda: supervisor._authenticator.invalidate.called)
        supervisor._authenticator.invalidate.assert_called_once_with('tok-xyz')
        return True


@pytest.mark.parametrize('value', ['three', 0, -1])
def test_invalid_process_workers_rejected(value):
    spawn, _ = _spawn_factory()
    listener, _ = make_tcp_listener()
    supervisor = Supervisor(
        {'process_workers': value},
        listener,
        Mock(),
        MasterTenantProxy.proxy,
        spawn=spawn,
    )
    try:
        with pytest.raises(ValueError):
            run_async(supervisor.__aenter__())
    finally:
        listener.close()
