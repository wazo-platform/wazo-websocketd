# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import itertools
from signal import SIGKILL, SIGTERM
from unittest.mock import AsyncMock, call, patch

import pytest

from .. import supervisor as supervisor_module
from ..scaling import ScalingPolicy
from ..supervisor import BrokerSupervisor, Supervisor, WorkerPool

MASTER_TENANT = 'aaaaaaaa-0000-4000-8000-tenanttenant'


class _FakeRenewer:
    """Answers with a service token as soon as anyone subscribes."""

    def __init__(self, _config):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def stop(self, *_args):
        pass

    def subscribe(self, callback, *, details=False, oneshot=False):
        if details:
            callback({'metadata': {'tenant_uuid': MASTER_TENANT}})


@pytest.fixture(autouse=True)
def runtime_dir(tmp_path):
    with patch.object(supervisor_module, 'RUN_DIR', str(tmp_path)), patch.object(
        supervisor_module, 'ServiceTokenRenewer', _FakeRenewer
    ):
        yield str(tmp_path)

    patch.stopall()


_CONFIG = {
    'config_file': '/etc/wazo-websocketd/config.yml',
    'debug': False,
    'process_workers': 2,
}


class FakeProcess:
    _pids = itertools.count(100)

    def __init__(self, command):
        self.command = command
        self.pid = next(self._pids)
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.exits_on_terminate = True
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def send_signal(self, signal):
        self._reject_when_gone()
        if signal == SIGKILL:
            self.killed = True
            self.exit(-SIGKILL)
        else:
            self.terminated = True
            if self.exits_on_terminate:
                self.exit(0)

    def terminate(self):
        self.send_signal(SIGTERM)

    def kill(self):
        self.send_signal(SIGKILL)

    def _reject_when_gone(self):
        if self.returncode is not None:
            raise ProcessLookupError

    def exit(self, code):
        self.returncode = code
        self._done.set()


class Harness:
    def __init__(self, config=_CONFIG):
        # RUN_DIR is patched per test, so resolve the socket path now
        config = {
            'broker': {'listen': f'{supervisor_module.RUN_DIR}/broker.sock'},
            **config,
        }
        self.processes = []
        self.statuses = {}
        patch('wazo_websocketd.helpers.process.spawn', self._spawn).start()
        self.supervisor = Supervisor(config)
        self.supervisor._status_client.fetch = self._fetch  # type: ignore[method-assign]

    async def _spawn(self, command):
        child = FakeProcess(command)
        self.processes.append(child)
        return child

    async def _fetch(self, socket_path):
        return self.statuses.get(socket_path)

    def spawned_commands(self):
        return [process.command for process in self.processes]

    def workers(self):
        return [
            process
            for process in self.processes
            if process.command[0] == 'wazo-websocketd-worker'
        ]

    def brokers(self):
        return [
            process
            for process in self.processes
            if process.command[0] == 'wazo-websocketd-broker'
        ]

    def sock(self, worker_id):
        return f'{supervisor_module.RUN_DIR}/{worker_id}.sock'

    @staticmethod
    def name_of(process):
        return process.command[process.command.index('--supervised-as') + 1]

    def report_drains(self):
        for entry in self.supervisor.registry.workers():
            if entry.draining and (status := self.statuses.get(entry.socket_path)):
                status['state'] = 'draining'

    async def reap_drained(self):
        # a watcher only removes its worker once the event loop resumes it
        await asyncio.gather(
            *(
                entry.wait_task
                for entry in self.supervisor.registry.workers()
                if entry.wait_task is not None
                and entry.process is not None
                and entry.process.returncode is not None
            )
        )

    def silence(self, worker_id):
        del self.statuses[self.sock(worker_id)]
        entry = self.supervisor.registry.get(worker_id)
        assert entry is not None
        entry.added_at -= 60
        return entry

    def report_all_ready(self):
        for process in self.workers():
            worker_id = self.name_of(process)
            self.statuses[self.sock(worker_id)] = {
                'state': 'ready',
                'connections': 0,
                'name': worker_id,
                'pid': process.pid,
            }


_SCALING_CONFIG = {
    'config_file': '/etc/wazo-websocketd/config.yml',
    'debug': False,
    'min_workers': 2,
    'max_workers': 4,
    'broker': {'listen': None},  # scaling tests supervise workers only
}


def _watermarks(high, low):
    return patch.multiple(ScalingPolicy, _HIGH_WATERMARK=high, _LOW_WATERMARK=low)


def _scaling_harness(config=_SCALING_CONFIG, cooldown=0.0):
    # the policy reads its cooldown once, when the pool builds it
    with patch.object(ScalingPolicy, '_SCALE_DOWN_COOLDOWN', cooldown):
        return Harness(config)


def _set_connections(harness, **connections_by_id):
    for worker_id, connections in connections_by_id.items():
        harness.statuses[harness.sock(worker_id)]['connections'] = connections


class TestStartup:
    async def test_start_spawns_the_broker_and_the_desired_workers(self):
        harness = Harness()
        await harness.supervisor.start()
        await harness.supervisor.stop()
        commands = harness.spawned_commands()
        assert commands[0][0] == 'wazo-websocketd-broker'
        worker_commands = commands[1:]
        assert [command[0] for command in worker_commands] == [
            'wazo-websocketd-worker'
        ] * 2
        assert [harness.name_of(p) for p in harness.workers()] == [
            'worker-1',
            'worker-2',
        ]

    async def test_start_skips_the_broker_when_listen_is_empty(self):
        harness = Harness({**_CONFIG, 'broker': {'listen': None}})
        await harness.supervisor.start()
        await harness.supervisor.poll()
        await harness.supervisor.stop()
        commands = harness.spawned_commands()
        assert [command[0] for command in commands] == ['wazo-websocketd-worker'] * 2

    async def test_workers_are_spawned_with_the_resolved_master_tenant(self):
        harness = Harness()
        await harness.supervisor.start()
        await harness.supervisor.stop()

        for worker in harness.workers():
            index = worker.command.index('--master-tenant')
            assert worker.command[index + 1] == MASTER_TENANT

    async def test_the_broker_is_not_given_a_master_tenant(self):
        harness = Harness()
        await harness.supervisor.start()
        await harness.supervisor.stop()
        assert '--master-tenant' not in harness.brokers()[0].command

    async def test_children_are_spawned_with_the_config_file(self):
        harness = Harness()
        await harness.supervisor.start()
        await harness.supervisor.stop()

        for command in harness.spawned_commands():
            assert command[command.index('-c') + 1] == '/etc/wazo-websocketd/config.yml'


class TestWorkerHealth:
    async def test_polling_feeds_the_registry_gauges(self):
        harness = Harness()
        await harness.supervisor.start()
        harness.report_all_ready()
        harness.statuses[harness.sock('worker-1')]['connections'] = 42
        await harness.supervisor.poll()
        total = harness.supervisor.registry.total_connections()
        await harness.supervisor.stop()
        result = total
        assert result == 42

    async def test_statuses_are_polled_concurrently(self):
        harness = Harness(_SCALING_CONFIG)
        concurrent = 0
        peak = 0

        async def slow_fetch(socket_path):
            nonlocal concurrent, peak
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.02)
            concurrent -= 1
            return harness.statuses.get(socket_path)

        await harness.supervisor.start()
        harness.report_all_ready()
        harness.supervisor._status_client.fetch = slow_fetch  # type: ignore[method-assign]
        await harness.supervisor.poll()
        await harness.supervisor.stop()
        assert peak > 1

    async def test_a_starting_worker_is_not_counted_as_active(self):
        harness = Harness()
        await harness.supervisor.start()
        harness.report_all_ready()
        harness.statuses[harness.sock('worker-1')]['state'] = 'starting'
        await harness.supervisor.poll()
        active = [e.worker_id for e in harness.supervisor.registry.active_workers()]
        await harness.supervisor.stop()
        result = active
        assert result == ['worker-2']

    async def test_an_unresponsive_worker_is_asked_to_drain_before_being_killed(self):
        harness = Harness()
        await harness.supervisor.start()
        harness.report_all_ready()
        harness.silence('worker-1')
        await harness.supervisor.poll()
        await asyncio.sleep(0.01)
        victim = harness.workers()[0]
        signals = (victim.terminated, victim.killed)
        await harness.supervisor.stop()
        result = signals
        terminated, killed = result
        assert terminated is True  # given a chance to close sessions with 1001
        assert killed is False

    async def test_an_unresponsive_worker_that_ignores_sigterm_is_killed(self):
        harness = Harness()
        await harness.supervisor.start()
        harness.report_all_ready()
        harness.silence('worker-1')
        victim = harness.workers()[0]
        victim.exits_on_terminate = False

        with patch.object(WorkerPool, '_KILL_GRACE', 0.0):
            await harness.supervisor.poll()
            await asyncio.sleep(0.05)
        killed = victim.killed
        await harness.supervisor.stop()
        result = killed
        assert result is True

    async def test_an_unresponsive_worker_is_replaced(self):
        harness = Harness()
        await harness.supervisor.start()
        harness.report_all_ready()
        harness.silence('worker-1')
        await harness.supervisor.poll()
        await asyncio.sleep(0.01)
        await harness.supervisor.stop()
        victim = harness.workers()[0]
        assert victim.returncode is not None  # stopped; how is covered separately
        worker_ids = [harness.name_of(process) for process in harness.workers()]
        assert worker_ids == ['worker-1', 'worker-2', 'worker-3']

    async def test_a_silent_worker_is_only_stopped_once(self):
        harness = Harness()
        await harness.supervisor.start()
        harness.report_all_ready()
        harness.silence('worker-1')
        victim = harness.workers()[0]
        victim.exits_on_terminate = False

        for _ in range(3):
            await harness.supervisor.poll()
            await asyncio.sleep(0.01)

        stops = harness.supervisor.registry.get('worker-1') is None
        await harness.supervisor.stop()
        result = stops, len(harness.workers())
        gone_from_registry, spawned = result
        assert gone_from_registry is False  # still tracked, marked as stopping
        assert spawned == 3  # 2 initial plus one replacement, not one per poll

    async def test_the_whole_pool_going_silent_is_not_killed(self):
        harness = Harness()
        await harness.supervisor.start()
        harness.report_all_ready()
        harness.statuses.clear()  # every probe fails: the supervisor stalled
        for entry in harness.supervisor.registry.workers():
            entry.added_at -= 60
            entry.last_seen = None
        await harness.supervisor.poll()
        await asyncio.sleep(0.01)
        killed = [p.killed for p in harness.workers()]
        await harness.supervisor.stop()
        result = killed
        assert result == [False, False]

    async def test_a_reaped_worker_has_its_status_session_closed(self):
        harness = Harness()
        forget = AsyncMock()
        client = harness.supervisor._status_client
        client.forget = forget  # type: ignore[method-assign]
        await harness.supervisor.start()
        harness.report_all_ready()
        await harness.supervisor.poll()

        harness.workers()[0].exit(0)
        await asyncio.sleep(0.01)

        await harness.supervisor.stop()
        # nothing polls that socket again, so the reap is our only chance to close it
        assert forget.await_args_list == [call(harness.sock('worker-1'))]

    async def test_a_stopped_worker_leaves_the_registry_without_a_watcher(self):
        harness = Harness()
        await harness.supervisor.start()
        harness.report_all_ready()
        entry = harness.supervisor.registry.get('worker-1')
        assert entry is not None and entry.wait_task is not None

        entry.wait_task.cancel()  # nobody is left to report its exit
        await asyncio.sleep(0)
        harness.workers()[0].exit(0)

        await harness.supervisor._workers._stop(entry)
        gone = harness.supervisor.registry.get('worker-1')
        await harness.supervisor.stop()
        result = gone
        assert result is None


class TestCrashBudget:
    async def test_a_dead_worker_is_replaced_under_a_new_id(self):
        harness = Harness()
        await harness.supervisor.start()
        with patch.object(WorkerPool, '_RESPAWN_DELAY', 0.0):
            harness.workers()[0].exit(1)
            await asyncio.sleep(0.01)
            await harness.supervisor.poll()
        await harness.supervisor.stop()
        worker_ids = [harness.name_of(process) for process in harness.workers()]
        assert worker_ids == ['worker-1', 'worker-2', 'worker-3']

    async def test_workers_crashing_on_startup_stop_being_respawned(self):
        harness = Harness()
        await harness.supervisor.start()
        with patch.object(WorkerPool, '_RESPAWN_DELAY', 0.0):
            for _ in range(WorkerPool._CRASH_BURST + 4):
                for process in harness.workers():
                    if process.returncode is None:
                        process.exit(1)  # fatal config error: dies immediately
                await asyncio.sleep(0.01)
                await harness.supervisor.poll()
        spawned = len(harness.workers())
        await harness.supervisor.stop()
        result = spawned, harness.supervisor.failed
        spawned, failed = result
        assert failed is True
        assert spawned <= WorkerPool._CRASH_BURST + 2  # 2 initial, then the budget

    async def test_workers_we_asked_to_drain_do_not_spend_the_crash_budget(self):
        with patch.object(WorkerPool, '_CRASH_BURST', 1):  # the pool reads it once
            harness = Harness(_SCALING_CONFIG)
        for _ in range(2):
            harness.supervisor.increase()
        await harness.supervisor.start()
        harness.report_all_ready()

        for _ in range(2):
            harness.supervisor.decrease()
        await asyncio.sleep(0.01)

        failed = harness.supervisor.failed
        await harness.supervisor.stop()
        result = failed
        # a planned drain is not a fault, however many the operator asks for
        assert result is False

    async def test_workers_we_stopped_for_silence_do_not_spend_the_crash_budget(self):
        with patch.object(WorkerPool, '_CRASH_BURST', 1):
            harness = Harness({**_CONFIG, 'process_workers': 3})
        await harness.supervisor.start()
        harness.report_all_ready()
        for worker_id in ('worker-1', 'worker-2'):  # a partial stall
            harness.silence(worker_id)
        await harness.supervisor.poll()
        await asyncio.sleep(0.01)

        failed = harness.supervisor.failed
        await harness.supervisor.stop()
        result = failed
        # stopping a silent worker is the supervisor's own doing, not a crash
        assert result is False


class TestScaling:
    async def test_scale_up_when_average_breaches_the_high_watermark(self):
        harness = Harness(_SCALING_CONFIG)
        await harness.supervisor.start()
        harness.report_all_ready()
        _set_connections(harness, **{'worker-1': 20, 'worker-2': 30})
        with _watermarks(high=10, low=5):
            await harness.supervisor.poll()
        count = len(harness.workers())
        await harness.supervisor.stop()
        result = count
        assert result == 3

    async def test_scale_up_is_one_worker_per_tick_and_stops_at_max(self):
        harness = Harness(_SCALING_CONFIG)
        await harness.supervisor.start()
        counts = []
        with _watermarks(high=10, low=5):
            for _ in range(4):
                harness.report_all_ready()
                for process in harness.workers():
                    _set_connections(harness, **{harness.name_of(process): 1000})
                await harness.supervisor.poll()
                counts.append(len(harness.workers()))
        await harness.supervisor.stop()
        result = counts
        assert result == [3, 4, 4, 4]

    async def test_scale_down_drains_the_least_loaded_worker_after_cooldown(self):
        harness = _scaling_harness()
        await harness.supervisor.start()
        harness.supervisor.increase()
        await harness.supervisor.poll()
        harness.report_all_ready()
        _set_connections(harness, **{'worker-1': 3, 'worker-2': 1, 'worker-3': 4})
        with _watermarks(high=10, low=5):
            await harness.supervisor.poll()
        await asyncio.sleep(0.01)
        count = len([w for w in harness.workers() if w.returncode is None])
        await harness.supervisor.stop()
        result = count
        assert result == 2
        drained = harness.workers()[1]
        assert harness.name_of(drained) == 'worker-2'
        assert drained.terminated is True

    async def test_no_scale_down_below_min_workers(self):
        harness = _scaling_harness()
        await harness.supervisor.start()
        harness.report_all_ready()
        _set_connections(harness, **{'worker-1': 0, 'worker-2': 0})
        with _watermarks(high=10, low=5):
            await harness.supervisor.poll()
        alive = len([w for w in harness.workers() if w.returncode is None])
        await harness.supervisor.stop()
        result = alive
        assert result == 2

    async def test_no_scale_down_when_survivors_would_breach_the_high_watermark(self):
        harness = _scaling_harness()
        await harness.supervisor.start()
        harness.supervisor.increase()
        await harness.supervisor.poll()
        harness.report_all_ready()
        _set_connections(harness, **{'worker-1': 9, 'worker-2': 9, 'worker-3': 9})
        with _watermarks(high=10, low=15):
            await harness.supervisor.poll()
        alive = len([w for w in harness.workers() if w.returncode is None])
        await harness.supervisor.stop()
        result = alive
        assert result == 3

    async def test_no_scale_down_while_another_drain_is_in_progress(self):
        harness = _scaling_harness()
        await harness.supervisor.start()
        harness.supervisor.increase()
        await harness.supervisor.poll()
        harness.report_all_ready()
        _set_connections(harness, **{'worker-1': 0, 'worker-2': 0, 'worker-3': 0})
        harness.statuses[harness.sock('worker-3')]['state'] = 'draining'
        with _watermarks(high=10, low=5):
            await harness.supervisor.poll()
        terminated = [w for w in harness.workers() if w.terminated]
        await harness.supervisor.stop()
        result = len(terminated)
        assert result == 0

    async def test_no_scale_down_while_a_drain_we_asked_for_is_unacknowledged(self):
        harness = _scaling_harness()
        await harness.supervisor.start()
        for _ in range(2):  # room to shrink twice before hitting min_workers
            harness.supervisor.increase()
        await harness.supervisor.poll()
        harness.report_all_ready()
        for worker in harness.workers():
            worker.exits_on_terminate = False  # SIGTERM not processed yet
        for worker in harness.workers():
            _set_connections(harness, **{harness.name_of(worker): 0})
        desired = []
        with _watermarks(high=10, low=5):
            for _ in range(2):
                await harness.supervisor.poll()
                desired.append(harness.supervisor._workers.desired)
        await harness.supervisor.stop()
        result = desired
        assert result == [3, 3]

    async def test_a_draining_worker_does_not_count_towards_the_desired_pool(self):
        harness = Harness()
        await harness.supervisor.start()
        harness.report_all_ready()
        harness.statuses[harness.sock('worker-1')]['state'] = 'draining'
        await harness.supervisor.poll()
        await asyncio.sleep(0.01)
        spawned = len(harness.workers())
        await harness.supervisor.stop()
        result = spawned
        # worker-1 is draining, so a third must be spawned to keep 2 serving
        assert result == 3

    async def test_workers_beyond_the_desired_pool_are_shed_one_at_a_time(
        self, tmp_path
    ):
        # bounds shrank across a re-exec: more children survive than are wanted
        for index in range(1, 5):
            (tmp_path / f'worker-{index}.sock').touch()

        harness = Harness()
        for index in range(1, 5):
            harness.statuses[str(tmp_path / f'worker-{index}.sock')] = {
                'state': 'ready',
                'connections': index,
                'name': f'worker-{index}',
                'pid': 4240 + index,
            }

        def serving():
            return sorted(
                entry.worker_id
                for entry in harness.supervisor.registry.workers()
                if not entry.draining
            )

        with patch('wazo_websocketd.supervisor.os.kill'):
            await harness.supervisor.start()
            adopted = len(harness.supervisor.registry.workers())
            after_start = serving()

            harness.report_drains()  # the worker obeys and reports draining
            for _ in range(3):
                await harness.supervisor.poll()
            while_draining = serving()

        await harness.supervisor.stop()
        result = adopted, after_start, while_draining
        adopted, after_start, while_draining = result
        assert adopted == 4
        assert after_start == ['worker-2', 'worker-3', 'worker-4']  # least loaded first
        assert while_draining == after_start  # no second drain until the first finishes

    async def test_nudges_are_clamped_to_the_pool_bounds(self):
        harness = Harness(_SCALING_CONFIG)
        await harness.supervisor.start()
        for _ in range(5):
            harness.supervisor.increase()
        await harness.supervisor.poll()
        grown = len(harness.workers())
        harness.report_all_ready()
        for _ in range(5):
            harness.supervisor.decrease()
        for _ in range(3):  # the pool sheds one worker per pass
            await harness.supervisor.poll()
            await harness.reap_drained()
        alive = len([w for w in harness.workers() if w.returncode is None])
        await harness.supervisor.stop()
        result = grown, alive
        grown, alive = result
        assert grown == 4
        assert alive == 2


class TestBrokerSupervision:
    async def test_broker_health_check_uses_the_configured_socket(self, tmp_path):
        socket_path = str(tmp_path / 'ws-broker.sock')
        harness = Harness({**_CONFIG, 'broker': {'listen': socket_path}})
        harness.statuses[socket_path] = {
            'state': 'ready',
            'connections': 0,
            'name': 'broker',
            'pid': 4242,
        }
        await harness.supervisor.start()
        for _ in range(4):  # more polls than the broker tolerates misses
            await harness.supervisor.poll()
        killed = [p.killed for p in harness.brokers()]
        await harness.supervisor.stop()
        result = killed
        killed = result
        assert killed == [False]  # the healthy broker was never replaced

    async def test_an_unresponsive_broker_is_replaced(self):
        harness = Harness()
        await harness.supervisor.start()
        harness.report_all_ready()
        with patch.object(BrokerSupervisor, '_RESPAWN_DELAY', 0.0):
            for _ in range(3):
                await harness.supervisor.poll()
            await asyncio.sleep(0.01)
        brokers = harness.brokers()
        await harness.supervisor.stop()
        result = brokers
        brokers = result
        assert len(brokers) == 2
        assert brokers[0].killed is True

    async def test_a_health_check_does_not_race_the_brokers_own_respawn(self):
        harness = Harness()
        await harness.supervisor.start()
        harness.report_all_ready()
        with patch.object(BrokerSupervisor, '_RESPAWN_DELAY', 0.05):
            harness.brokers()[0].exit(1)
            await asyncio.sleep(0.01)  # the watcher is sleeping before respawning
            for _ in range(3):  # more polls than the broker tolerates misses
                await harness.supervisor.poll()
            during_the_window = len(harness.brokers())
            await asyncio.sleep(0.1)
        brokers = harness.brokers()
        await harness.supervisor.stop()
        result = during_the_window, brokers
        during_the_window, brokers = result
        assert during_the_window == 1  # the watcher still owns the respawn
        assert len(brokers) == 2

    async def test_stop_terminates_a_broker_whose_respawn_was_in_flight(self):
        harness = Harness()
        release = asyncio.Event()
        await harness.supervisor.start()

        async def slow_spawn(command):
            await release.wait()
            return await harness._spawn(command)

        with patch.object(BrokerSupervisor, '_RESPAWN_DELAY', 0.0), patch(
            'wazo_websocketd.helpers.process.spawn', slow_spawn
        ):
            harness.brokers()[0].exit(1)
            await asyncio.sleep(0.01)  # the respawn is now blocked inside the spawn

            stopping = asyncio.ensure_future(harness.supervisor.stop())
            await asyncio.sleep(0.01)
            release.set()  # the replacement appears after stop() began
            await stopping
            await asyncio.sleep(0.01)  # let the in-flight spawn register

        result = harness.brokers()
        brokers = result
        assert len(brokers) == 2
        assert brokers[-1].terminated is True

    async def test_no_broker_is_spawned_once_shutdown_has_begun(self):
        harness = Harness()
        await harness.supervisor.start()
        await harness.supervisor.stop()
        # a health check that was already replacing an unresponsive broker
        await harness.supervisor._broker._spawn()
        result = harness.brokers()
        assert len(result) == 1


class TestAdoption:
    async def test_start_adopts_running_children_from_their_status_sockets(
        self, tmp_path
    ):
        for name in ('worker-3.sock', 'worker-9.sock', 'broker.sock', 'dead.sock'):
            (tmp_path / name).touch()

        harness = Harness()
        harness.statuses.update(
            {
                str(tmp_path / 'worker-3.sock'): {
                    'state': 'ready',
                    'connections': 11,
                    'name': 'worker-3',
                    'pid': 300,
                },
                str(tmp_path / 'worker-9.sock'): {
                    'state': 'ready',
                    'connections': 4,
                    'name': 'worker-9',
                    'pid': 900,
                },
                str(tmp_path / 'broker.sock'): {
                    'state': 'ready',
                    'connections': 0,
                    'name': 'broker',
                    'pid': 77,
                },
            }
        )
        await harness.supervisor.start()
        adopted = [entry.worker_id for entry in harness.supervisor.registry.workers()]
        pids = {
            entry.worker_id: entry.pid
            for entry in harness.supervisor.registry.workers()
        }
        await harness.supervisor.stop()
        result = adopted, pids
        adopted, pids = result
        assert sorted(adopted) == ['worker-3', 'worker-9']
        assert pids == {'worker-3': 300, 'worker-9': 900}
        assert not (tmp_path / 'dead.sock').exists()
        assert harness.spawned_commands() == []  # broker adopted, pool complete

    async def test_a_live_but_silent_child_is_adopted_not_unlinked(self, tmp_path):
        worker_socket = tmp_path / 'worker-2.sock'
        broker_socket = tmp_path / 'broker.sock'
        for path in (worker_socket, broker_socket):
            path.touch()
        harness = Harness()
        with patch(
            'wazo_websocketd.supervisor.socket_peer_pid', side_effect=[7070, 8080]
        ):
            await harness.supervisor.start()
        adopted = {
            entry.worker_id: entry.pid
            for entry in harness.supervisor.registry.workers()
        }
        await harness.supervisor.stop()

        assert worker_socket.exists()
        assert broker_socket.exists()
        assert adopted['worker-2'] == 8080
        assert [command[0] for command in harness.spawned_commands()] == [
            'wazo-websocketd-worker'
        ]

    async def test_a_socket_with_no_listener_is_removed(self, tmp_path):
        (tmp_path / 'worker-2.sock').touch()
        harness = Harness()
        with patch('wazo_websocketd.supervisor.socket_peer_pid', return_value=None):
            await harness.supervisor.start()
        await harness.supervisor.stop()
        assert not (tmp_path / 'worker-2.sock').exists()

    async def test_spawns_after_adoption_continue_the_numbering(self, tmp_path):
        (tmp_path / 'worker-9.sock').touch()
        harness = Harness()
        harness.statuses[str(tmp_path / 'worker-9.sock')] = {
            'state': 'ready',
            'connections': 0,
            'name': 'worker-9',
            'pid': 900,
        }
        await harness.supervisor.start()
        await harness.supervisor.stop()
        worker_ids = [harness.name_of(process) for process in harness.workers()]
        assert worker_ids == ['worker-10']

    def test_reexec_replaces_the_process_image_in_place(self):
        harness = Harness()

        with patch('wazo_websocketd.supervisor.os.execvp') as execvp:
            harness.supervisor.reexec()

        execvp.assert_called_once()
        executable, argv = execvp.call_args.args
        assert argv[0] == executable

    async def test_an_unresponsive_adopted_worker_is_killed_by_pid(self, tmp_path):
        (tmp_path / 'worker-4.sock').touch()
        harness = Harness()
        harness.statuses[str(tmp_path / 'worker-4.sock')] = {
            'state': 'ready',
            'connections': 0,
            'name': 'worker-4',
            'pid': 4242,
        }
        await harness.supervisor.start()
        del harness.statuses[str(tmp_path / 'worker-4.sock')]
        adopted = harness.supervisor.registry.workers()[0]
        adopted.last_seen = 0.0
        with patch('wazo_websocketd.supervisor.os.kill') as kill:
            await harness.supervisor.poll()
            await asyncio.sleep(0.01)  # the stop runs as a job
            # signal 0 is the liveness probe, not a stop request
            signalled = [c for c in kill.call_args_list if c.args[1] != 0]
        await harness.supervisor.stop()
        result = signalled
        calls = result
        assert [call.args[0] for call in calls] == [4242]

    async def test_adopted_workers_are_reaped_when_they_exit(self, tmp_path):
        (tmp_path / 'worker-4.sock').touch()
        harness = Harness()
        harness.statuses[str(tmp_path / 'worker-4.sock')] = {
            'state': 'ready',
            'connections': 0,
            'name': 'worker-4',
            'pid': 4242,
        }
        reaped = []

        def fake_waitpid(pid, flags):
            reaped.append(pid)
            return (pid, 0)

        with patch('wazo_websocketd.supervisor.os.waitpid', fake_waitpid):
            with patch.object(WorkerPool, '_REAP_INTERVAL', 0.0):
                await harness.supervisor.start()
                await asyncio.sleep(0.05)
        await harness.supervisor.stop()
        assert 4242 in reaped
        assert harness.supervisor.registry.get('worker-4') is None

    async def test_an_adopted_worker_reaped_by_someone_else_still_leaves_the_registry(
        self, tmp_path
    ):
        (tmp_path / 'worker-4.sock').touch()
        harness = Harness()
        harness.statuses[str(tmp_path / 'worker-4.sock')] = {
            'state': 'ready',
            'connections': 0,
            'name': 'worker-4',
            'pid': 4242,
        }

        def already_reaped(pid, flags):
            raise ChildProcessError

        with patch('wazo_websocketd.supervisor.os.waitpid', already_reaped), patch(
            'wazo_websocketd.helpers.process.os.kill', side_effect=OSError
        ), patch.object(WorkerPool, '_REAP_INTERVAL', 0.0):
            await harness.supervisor.start()
            await asyncio.sleep(0.05)
        await harness.supervisor.stop()
        result = harness.supervisor.registry.get('worker-4')
        # the pid is gone, so waitpid failing means we lost the race, not that it lives
        assert result is None


class TestShutdown:
    async def test_stop_terminates_every_child(self):
        harness = Harness()
        await harness.supervisor.start()
        await harness.supervisor.stop()
        assert all(process.terminated for process in harness.processes)

    async def test_stop_terminates_a_worker_whose_spawn_was_in_flight(self):
        harness = Harness(_SCALING_CONFIG)
        release = asyncio.Event()
        await harness.supervisor.start()

        async def slow_spawn(command):
            await release.wait()
            return await harness._spawn(command)

        with patch('wazo_websocketd.helpers.process.spawn', slow_spawn):
            harness.supervisor.increase()
            polling = asyncio.ensure_future(harness.supervisor.poll())
            await asyncio.sleep(0.01)  # the reconcile is now blocked inside the spawn

            stopping = asyncio.ensure_future(harness.supervisor.stop())
            await asyncio.sleep(0.01)
            release.set()  # the child appears after stop() began
            await asyncio.gather(polling, stopping)

        result = harness.workers()
        workers = result
        assert len(workers) == 3
        assert workers[-1].terminated is True

    async def test_stop_still_terminates_the_children_that_outlived_a_dead_sibling(
        self,
    ):
        harness = Harness()
        await harness.supervisor.start()
        dead = harness.workers()[0]
        dead.exit(0)
        await harness.supervisor.stop()

        assert dead.terminated is False
        assert all(
            process.terminated for process in harness.processes if process is not dead
        )

    async def test_shutdown_stops_children_and_adopted_workers_at_once(self):
        harness = Harness(_SCALING_CONFIG)  # no broker: only the pool stops here
        order = []

        async def slow_stop_pids(pids, **_kwargs):
            order.append('adopted:start')
            await asyncio.sleep(0.05)
            order.append('adopted:done')

        async def slow_stop_children(children, **_kwargs):
            order.append('children:start')
            await asyncio.sleep(0.05)
            order.append('children:done')

        await harness.supervisor.start()
        with patch('wazo_websocketd.helpers.process.stop_pids', slow_stop_pids), patch(
            'wazo_websocketd.helpers.process.stop_children', slow_stop_children
        ):
            await harness.supervisor.stop()
        # both graces run at once, so a mixed pool costs one of them, not two
        assert order == [
            'adopted:start',
            'children:start',
            'adopted:done',
            'children:done',
        ]
