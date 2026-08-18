# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import sys
import time
from contextlib import AsyncExitStack
from glob import glob
from operator import attrgetter
from signal import SIGINT, SIGKILL, SIGTERM, SIGTTIN, SIGTTOU, SIGUSR2

from .config import is_unix_socket
from .exception import CrashBudgetExhausted
from .helpers import process
from .helpers.tasks import is_pending
from .helpers.timing import CrashBucket
from .registry import WorkerEntry, WorkerRegistry
from .scaling import ScalingPolicy
from .status import BROKER_ID, RUN_DIR, StatusClient, WorkerState, socket_peer_pid
from .token_renewer import ServiceTokenRenewer

logger = logging.getLogger(__name__)


def prepare_runtime_dir(user: str | None) -> None:
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        if user and process.is_root():
            shutil.chown(RUN_DIR, user=user, group=None)
    except OSError as e:
        logger.warning('could not prepare the runtime directory %s: %s', RUN_DIR, e)


class BrokerSupervisor:
    _MAX_MISSES = 3
    _RESPAWN_DELAY = 1.0
    _REAP_INTERVAL = 1.0
    _STOP_GRACE = 15.0

    def __init__(self, config: dict, status_client: StatusClient) -> None:
        self._config = config
        self._status_client = status_client
        self._closing = asyncio.Event()
        self._spawning_lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._pid: int | None = None
        self._misses = 0

        listen = config.get('broker', {}).get('listen')
        self._enabled = bool(listen)
        if listen and is_unix_socket(listen):
            self._socket = listen
        else:
            self._socket = f'{RUN_DIR}/{BROKER_ID}.sock'

    def adopt(self, pid: int) -> None:
        self._pid = pid
        logger.info('adopted running broker (pid %s)', pid)

    async def start(self) -> None:
        if self._enabled and self._pid is None:
            await self._spawn()

    async def check(self) -> None:
        if not self._enabled:
            return

        if await self._status_client.fetch(self._socket) is not None:
            self._misses = 0
            return

        self._misses += 1
        if self._misses < self._MAX_MISSES:
            return

        logger.warning('broker is unresponsive, replacing it')
        self._misses = 0
        if self._process is not None and self._process.returncode is None:
            process.signal_process(self._process, SIGKILL)  # its watcher respawns it
            return

        if self._respawn_pending():
            return

        if self._pid is not None:
            process.signal_pid(self._pid, SIGKILL)
            await self._wait_for_exit()
        await self._spawn()

    def _respawn_pending(self) -> bool:
        return is_pending(self._task)

    async def close(self) -> None:
        self._closing.set()
        async with self._spawning_lock:
            child, pid = self._process, self._pid

        if child is not None:
            process.signal_process(child)
            await process.stop_children([child], grace=self._STOP_GRACE, label='broker')
        elif pid is not None:
            process.signal_pid(pid)
            await process.stop_pids(
                [pid],
                grace=self._STOP_GRACE,
                interval=self._REAP_INTERVAL,
                label='broker',
            )

    async def _spawn(self) -> None:
        async with self._spawning_lock:
            if self._closing.is_set():
                return

            self._process = await process.spawn(
                process.child_command(self._config, 'wazo-websocketd-broker', BROKER_ID)
            )
            self._pid = self._process.pid
            self._misses = 0
            self._task = asyncio.get_event_loop().create_task(
                self._watch(self._process)
            )
            logger.info('spawned broker (pid %s)', self._process.pid)

    async def _watch(self, child: asyncio.subprocess.Process) -> None:
        code = await child.wait()
        if self._closing.is_set():
            return

        logger.warning('broker exited with code %s, respawning it', code)
        await asyncio.sleep(self._RESPAWN_DELAY)
        if not self._closing.is_set():
            await self._spawn()

    async def _wait_for_exit(self) -> None:
        for _ in range(int(self._STOP_GRACE / self._REAP_INTERVAL)):
            if await socket_peer_pid(self._socket) is None:
                return
            await asyncio.sleep(self._REAP_INTERVAL)
        logger.warning('broker still holds its socket, spawning anyway')


class WorkerPool:
    _MAX_SILENCE = 15.0
    _STOP_GRACE = 15.0
    _SPAWN_GRACE = 5.0
    _KILL_GRACE = 5.0
    _CRASH_BURST = 15
    _CRASH_WINDOW = 150.0
    _RESPAWN_DELAY = 1.0
    _REAP_INTERVAL = 1.0

    def __init__(self, config: dict, status_client: StatusClient) -> None:
        self._config = config
        self._status_client = status_client
        self._registry = WorkerRegistry()
        self._scaling = ScalingPolicy(config)
        self._closing = asyncio.Event()
        self._fatal = asyncio.Event()
        self._changed = asyncio.Event()
        self._spawning_lock = asyncio.Lock()
        self._jobs: set[asyncio.Task] = set()
        self._crashes = CrashBucket(burst=self._CRASH_BURST, window=self._CRASH_WINDOW)
        self._next_id = 1
        self._master_tenant: str | None = None

    @property
    def registry(self) -> WorkerRegistry:
        return self._registry

    @property
    def desired(self) -> int:
        return self._scaling.desired

    @property
    def fatal(self) -> asyncio.Event:
        return self._fatal

    @property
    def changed(self) -> asyncio.Event:
        return self._changed

    def adopt(self, socket_path: str, status: dict) -> None:
        worker_id = status['name']
        entry = self._registry.add(worker_id, socket_path)
        entry.pid = status['pid']
        self._registry.record_status(
            worker_id,
            pid=status['pid'],
            connections=status['connections'],
            state=status['state'],
        )
        entry.wait_task = asyncio.get_event_loop().create_task(
            self._reap_adopted(worker_id, status['pid'])
        )
        logger.info('adopted running worker `%s` (pid %s)', worker_id, entry.pid)

        if match := re.fullmatch(r'worker-(\d+)', worker_id):
            self._next_id = max(self._next_id, int(match.group(1)) + 1)

    async def start(self, master_tenant: str | None) -> None:
        self._master_tenant = master_tenant
        await self._reconcile()

    async def poll(self) -> None:
        self._changed.clear()
        workers = self._registry.workers()
        statuses = await asyncio.gather(
            *(self._status_client.fetch(entry.socket_path) for entry in workers)
        )
        for entry, status in zip(workers, statuses):
            if status is not None:
                self._registry.record_status(
                    entry.worker_id,
                    pid=status['pid'],
                    connections=status['connections'],
                    state=status['state'],
                )

        for entry in self._unresponsive():
            logger.warning('worker `%s` is unresponsive, stopping it', entry.worker_id)
            entry.stopping = True
            self._spawn_job(self._stop(entry))

        self._scaling.adjust(self._registry)
        await self._reconcile()

    async def close(self) -> None:
        self._closing.set()
        await self._wait_for_jobs()

        async with self._spawning_lock:
            entries = self._registry.workers()

        for entry in entries:
            entry.signal()

        adopted = [e.pid for e in entries if e.process is None and e.pid is not None]
        children = [e.process for e in entries if e.process is not None]
        await asyncio.gather(
            process.stop_pids(
                adopted,
                grace=self._STOP_GRACE,
                interval=self._REAP_INTERVAL,
                label='worker',
            ),
            process.stop_children(children, grace=self._STOP_GRACE, label='worker(s)'),
        )

    def increase(self) -> None:
        if self._scaling.increase():
            self._changed.set()

    def decrease(self) -> None:
        if self._scaling.decrease():
            self._changed.set()

    def _unresponsive(self) -> list[WorkerEntry]:
        unresponsive = self._registry.unresponsive(max_silence=self._MAX_SILENCE)
        if len(unresponsive) > 1 and len(unresponsive) == len(self._registry.workers()):
            logger.warning(
                'all %d workers went silent at once, blaming this supervisor '
                'rather than the pool and killing none of them',
                len(unresponsive),
            )
            return []
        return unresponsive

    def _drain_least_loaded(self) -> None:
        if candidates := self._registry.drainable():
            self._drain(min(candidates, key=attrgetter('connections')))

    def _drain(self, entry: WorkerEntry) -> None:
        logger.info('draining worker `%s`', entry.worker_id)
        entry.state = WorkerState.DRAINING
        entry.signal()

    async def _wait_for_jobs(self) -> None:
        if not (running := tuple(self._jobs)):
            return

        _, pending = await asyncio.wait(running, timeout=self._SPAWN_GRACE)
        if pending:
            logger.warning('%d spawn(s) still running at shutdown', len(pending))

    async def _stop(self, entry: WorkerEntry) -> None:
        if entry.is_running():
            entry.signal()

            deadline = time.monotonic() + self._KILL_GRACE
            while (
                not self._closing.is_set()
                and entry.is_running()
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(self._REAP_INTERVAL)

            # close() signals everything itself, so leave the escalation to it
            if not self._closing.is_set() and entry.is_running():
                logger.warning(
                    'worker `%s` ignored SIGTERM, killing it', entry.worker_id
                )
                entry.signal(SIGKILL)

        if not self._closing.is_set() and not entry.is_running() and not entry.watched:
            await self._drop(entry)

    async def _drop(self, entry: WorkerEntry) -> None:
        self._registry.remove(entry.worker_id)
        await self._status_client.forget(entry.socket_path)

    def _spawn_job(self, coro) -> None:
        task = asyncio.get_event_loop().create_task(coro)
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    async def _reconcile(self) -> None:
        async with self._spawning_lock:
            while (
                not self._closing.is_set()
                and len(self._registry.drainable()) < self._scaling.desired
            ):
                await self._spawn()

        if self._closing.is_set() or self._registry.has_draining():
            return

        if len(self._registry.drainable()) > self._scaling.desired:
            self._drain_least_loaded()

    async def _spawn(self) -> WorkerEntry:
        worker_id = f'worker-{self._next_id}'
        self._next_id += 1
        child = await process.spawn(self._command(worker_id))

        entry = self._registry.add(worker_id, f'{RUN_DIR}/{worker_id}.sock')
        entry.process = child
        entry.pid = child.pid
        entry.wait_task = asyncio.get_event_loop().create_task(
            self._watch(worker_id, child)
        )
        logger.info('spawned worker `%s` (pid %s)', worker_id, child.pid)
        return entry

    async def _watch(self, worker_id: str, child) -> None:
        code = await child.wait()
        if self._closing.is_set():
            return

        entry = self._registry.get(worker_id)
        if entry is not None:
            await self._drop(entry)
        if entry is not None and entry.leaving:
            logger.info('worker `%s` exited as asked', worker_id)
            self._changed.set()
            return

        logger.warning('worker `%s` exited with code %s', worker_id, code)
        try:
            self._crashes.record()
        except CrashBudgetExhausted as e:
            logger.error(
                '%s: giving up, the next start will not fare better without '
                'operator action',
                e,
            )
            self._closing.set()
            self._fatal.set()
            return

        await asyncio.sleep(self._RESPAWN_DELAY)
        self._changed.set()

    async def _reap_adopted(self, worker_id: str, pid: int) -> None:
        while not self._closing.is_set():
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                if process.is_pid_running(pid):
                    return  # reparented to init: the status poll is our only signal
                break  # somebody else reaped it first
            if reaped:
                break
            await asyncio.sleep(self._REAP_INTERVAL)

        if self._closing.is_set():
            return
        logger.warning('adopted worker `%s` exited', worker_id)
        if (entry := self._registry.get(worker_id)) is not None:
            await self._drop(entry)
        await asyncio.sleep(self._RESPAWN_DELAY)
        self._changed.set()

    def _command(self, worker_id: str) -> list[str]:
        command = process.child_command(
            self._config, 'wazo-websocketd-worker', worker_id
        )
        if self._master_tenant:
            command += ['--master-tenant', self._master_tenant]
        return command


class Supervisor:
    _POLL_INTERVAL = 5.0
    _IDLE_SESSION_POLLS = 12
    _MASTER_TENANT_GRACE = 5.0

    def __init__(self, config: dict) -> None:
        self._config = config
        self._status_client = StatusClient(
            idle_timeout=self._POLL_INTERVAL * self._IDLE_SESSION_POLLS
        )
        self._closing = asyncio.Event()
        self._reexec_requested = asyncio.Event()
        self._stack = AsyncExitStack()
        self._master_tenant: str | None = None
        self._master_tenant_known = asyncio.Event()
        self._broker = BrokerSupervisor(config, self._status_client)
        self._workers = WorkerPool(config, self._status_client)

    @property
    def registry(self) -> WorkerRegistry:
        return self._workers.registry

    @property
    def failed(self) -> bool:
        return self._workers.fatal.is_set()

    def increase(self) -> None:
        self._workers.increase()

    def decrease(self) -> None:
        self._workers.decrease()

    async def run(self) -> None:
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(SIGINT, self._closing.set)
        loop.add_signal_handler(SIGTERM, self._closing.set)
        loop.add_signal_handler(SIGTTIN, self.increase)
        loop.add_signal_handler(SIGTTOU, self.decrease)
        loop.add_signal_handler(SIGUSR2, self._reexec_requested.set)

        await self.start()
        logger.info(
            'wazo-websocketd supervisor started (%d workers)', self._workers.desired
        )

        while not self._closing.is_set():
            await self._wait_for_work()
            if self._reexec_requested.is_set():
                self.reexec()
            if self._closing.is_set() or self._workers.fatal.is_set():
                break
            await self.poll()

        await self.stop()
        logger.info('wazo-websocketd supervisor stopped')

    async def _wait_for_work(self) -> None:
        waiters = [
            asyncio.ensure_future(event.wait())
            for event in (
                self._closing,
                self._reexec_requested,
                self._workers.fatal,
                self._workers.changed,
            )
        ]
        try:
            await asyncio.wait(waiters, timeout=self._POLL_INTERVAL)
        finally:
            for waiter in waiters:
                waiter.cancel()

    def reexec(self) -> None:
        logger.info('re-executing the supervisor in place, children are preserved')
        self._reexec_requested.clear()
        try:
            os.execvp(sys.argv[0], sys.argv)
        except OSError as e:
            logger.error('could not re-execute %s: %s', sys.argv[0], e)

    async def start(self) -> None:
        await self._init_master_tenant()
        await self._adopt_children()
        await self._broker.start()
        await self._workers.start(self._master_tenant)

    async def _init_master_tenant(self) -> None:
        renewer = await self._stack.enter_async_context(
            ServiceTokenRenewer(self._config)
        )
        renewer.subscribe(self._remember_master_tenant, details=True, oneshot=True)
        renewer.subscribe(renewer.stop, oneshot=True)
        try:
            await asyncio.wait_for(
                self._master_tenant_known.wait(), self._MASTER_TENANT_GRACE
            )
        except TimeoutError:
            logger.warning(
                'no master tenant after %.0fs, workers will resolve their own',
                self._MASTER_TENANT_GRACE,
            )

    def _remember_master_tenant(self, token: dict) -> None:
        try:
            self._master_tenant = token['metadata']['tenant_uuid']
        except KeyError:
            logger.error('service token carries no tenant_uuid')
            return

        logger.info('master tenant resolved as `%s`', self._master_tenant)
        self._master_tenant_known.set()

    async def _adopt_children(self) -> None:
        for socket_path in sorted(glob(f'{RUN_DIR}/*.sock')):
            status = await self._status_client.fetch(
                socket_path
            ) or await self._probe_silent_child(socket_path)
            if status is None:
                logger.info('removing stale status socket %s', socket_path)
                with contextlib.suppress(OSError):
                    os.unlink(socket_path)
                continue

            if status['name'] == BROKER_ID:
                self._broker.adopt(status['pid'])
            else:
                self._workers.adopt(socket_path, status)

    @staticmethod
    async def _probe_silent_child(socket_path: str) -> dict | None:
        if (pid := await socket_peer_pid(socket_path)) is None:
            return None
        worker_id = os.path.basename(socket_path).removesuffix('.sock')
        logger.warning(
            'child `%s` (pid %s) is listening but did not answer /status',
            worker_id,
            pid,
        )
        return {
            'name': worker_id,
            'pid': pid,
            'connections': 0,
            'state': WorkerState.STARTING.value,
        }

    async def poll(self) -> None:
        await asyncio.gather(self._workers.poll(), self._broker.check())

    async def stop(self) -> None:
        self._closing.set()
        await asyncio.gather(self._broker.close(), self._workers.close())
        await self._status_client.close()
        await self._stack.aclose()
