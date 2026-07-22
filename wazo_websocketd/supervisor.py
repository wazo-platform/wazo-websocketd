# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import itertools
import logging
import socket
from collections.abc import Callable
from multiprocessing import get_context
from multiprocessing.context import SpawnProcess
from os import sched_getaffinity
from signal import SIGTTIN, SIGTTOU

from .auth import StringSharedBuffer, TokenAuthenticator
from .dispatcher import Dispatcher
from .ipc import ControlMessage, Heartbeat, Revoke
from .registry import WorkerEntry, WorkerRegistry
from .worker import Worker

logger = logging.getLogger(__name__)


class Supervisor:
    _PRUNE_INTERVAL = 5.0
    _MAX_HEARTBEAT_SILENCE = 10.0
    _JOIN_TIMEOUT = 35.0

    def __init__(
        self,
        config: dict,
        listener: socket.socket,
        authenticator: TokenAuthenticator,
        master_tenant_proxy: StringSharedBuffer,
        *,
        spawn: Callable[[socket.socket, str], SpawnProcess] | None = None,
    ) -> None:
        self._config = config
        self._loop = asyncio.get_event_loop()
        self._authenticator = authenticator
        self._registry = WorkerRegistry()
        self._dispatcher = Dispatcher(
            self._loop, listener, authenticator, self._registry
        )
        self._master_tenant_proxy = master_tenant_proxy
        self._context = get_context('spawn')
        self._spawn = spawn or self._spawn_process
        self._counter = itertools.count(1)
        self._desired = 0
        self._tasks: list[asyncio.Task] = []
        self._spawn_tasks: set[asyncio.Task] = set()
        self._spawn_lock = asyncio.Lock()
        self._closing = asyncio.Event()

    @property
    def registry(self) -> WorkerRegistry:
        return self._registry

    async def __aenter__(self) -> Supervisor:
        self._tasks.append(self._loop.create_task(self._dispatcher.run()))
        self._tasks.append(self._loop.create_task(self._prune_loop()))
        self._loop.add_signal_handler(SIGTTIN, self.add_worker)
        self._loop.add_signal_handler(SIGTTOU, self.scale_down)

        self._desired = self._initial_worker_count()
        await self._reconcile()

        return self

    async def __aexit__(self, *exc) -> None:
        self._closing.set()
        for signal in (SIGTTIN, SIGTTOU):
            self._loop.remove_signal_handler(signal)

        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.gather(*self._spawn_tasks, return_exceptions=True)
        for entry in self._registry.workers():
            self._terminate(entry)

        await self._loop.run_in_executor(None, self._join_all)

        for entry in self._registry.workers():
            if entry.heartbeat_task is not None:
                entry.heartbeat_task.cancel()
            entry.control_sock.close()

    def add_worker(self) -> None:
        self._desired += 1
        task = self._loop.create_task(self._reconcile())
        self._spawn_tasks.add(task)
        task.add_done_callback(self._spawn_task_done)

    def _spawn_task_done(self, task: asyncio.Task) -> None:
        self._spawn_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error('worker spawn task failed: %s', exc)

    async def _spawn_worker(self) -> str:
        worker_id = f'worker-{next(self._counter)}'
        dispatcher_sock, worker_sock = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_SEQPACKET
        )
        process = await self._loop.run_in_executor(
            None, self._spawn, worker_sock, worker_id
        )
        worker_sock.close()  # the child holds its own dup
        dispatcher_sock.setblocking(False)
        entry = self._registry.add(worker_id, dispatcher_sock)
        entry.process = process
        entry.heartbeat_task = self._loop.create_task(
            self._read_heartbeats(worker_id, dispatcher_sock)
        )
        logger.info('spawned %s (pid %s)', worker_id, process.pid)
        return worker_id

    async def _read_heartbeats(
        self, worker_id: str, control_sock: socket.socket
    ) -> None:
        control_sock.setblocking(False)
        while True:
            try:
                message = await self._loop.sock_recv(control_sock, 4096)
            except OSError:
                break

            if not message:
                break

            try:
                match ControlMessage.unmarshal(message):
                    case Revoke(token_id):
                        self._authenticator.invalidate(token_id)
                    case Heartbeat(count):
                        self._registry.heartbeat(worker_id, count)
            except (ValueError, KeyError):
                logger.warning('ignoring malformed control message from %s', worker_id)

        if not self._closing.is_set():
            self._reap(worker_id)

    def scale_down(self) -> None:
        for worker in reversed(self._registry.workers()):
            if not worker.draining:
                self._desired = max(0, self._desired - 1)
                self.drain_worker(worker.worker_id)
                return
        logger.info('scale down requested but no drainable worker')

    def drain_worker(self, worker_id: str) -> None:
        if (worker := self._registry.get(worker_id)) is None:
            return

        self._registry.start_draining(worker_id)
        self._terminate(worker)
        logger.info('draining %s', worker_id)

    def _terminate(self, entry: WorkerEntry) -> None:
        if entry.process is not None and entry.process.is_alive():
            entry.process.terminate()

    def _spawn_process(
        self, worker_sock: socket.socket, worker_id: str
    ) -> SpawnProcess:
        process = self._context.Process(
            target=Worker.entrypoint,
            args=(self._config, worker_sock, self._master_tenant_proxy, worker_id),
            name=worker_id,
        )
        process.start()
        return process

    def _initial_worker_count(self) -> int:
        workers = self._config['process_workers']

        if workers == 'auto':
            return len(sched_getaffinity(0))

        if not isinstance(workers, int) or workers < 1:
            raise ValueError(
                'configuration key `process_workers` must be a positive '
                'integer or `auto`'
            )
        return workers

    def _join_all(self) -> None:
        for worker in self._registry.workers():
            process = worker.process
            if process is not None and process.is_alive():
                process.join(self._JOIN_TIMEOUT)
                if process.is_alive():
                    logger.warning('worker did not exit in time, killing...')
                    process.kill()
                    process.join()

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(self._PRUNE_INTERVAL)
            for worker_id in self._registry.unresponsive_workers(
                self._MAX_HEARTBEAT_SILENCE
            ):
                self._reap(worker_id)
            await self._reconcile()

    async def _reconcile(self) -> None:
        async with self._spawn_lock:
            if self._closing.is_set():
                return

            if (deficit := self._desired - self._active_workers_count()) > 0:
                logger.info(
                    'spawning %d worker(s) to reach desired count of %d',
                    deficit,
                    self._desired,
                )

            for _ in range(deficit):
                await self._spawn_worker()

    def _active_workers_count(self) -> int:
        return sum(1 for worker in self._registry.workers() if not worker.draining)

    def _reap(self, worker_id: str) -> None:
        entry = self._registry.remove(worker_id)
        if entry is None:
            return

        if entry.heartbeat_task is not None:
            entry.heartbeat_task.cancel()

        self._terminate(entry)
        entry.control_sock.close()
        logger.info('reaped %s', worker_id)
