# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from signal import SIGTERM

from .helpers.process import is_pid_running, signal_pid, signal_process
from .helpers.tasks import is_pending
from .status import WorkerState


@dataclass
class WorkerEntry:
    worker_id: str
    socket_path: str
    added_at: float
    pid: int | None = None
    process: asyncio.subprocess.Process | None = None
    connections: int = 0
    state: WorkerState = WorkerState.STARTING
    stopping: bool = False
    last_seen: float | None = None
    wait_task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def draining(self) -> bool:
        return self.state is WorkerState.DRAINING

    @property
    def leaving(self) -> bool:
        return self.draining or self.stopping

    @property
    def watched(self) -> bool:
        return is_pending(self.wait_task)

    @property
    def is_ready(self) -> bool:
        return self.state is WorkerState.READY

    def is_running(self) -> bool:
        if self.process is not None:
            return self.process.returncode is None
        return self.pid is not None and is_pid_running(self.pid)

    def signal(self, signal: int = SIGTERM) -> None:
        if self.process is not None:
            signal_process(self.process, signal)
        elif self.pid is not None:
            signal_pid(self.pid, signal)


class WorkerRegistry:
    def __init__(self, timer: Callable[[], float] = time.monotonic) -> None:
        self._timer = timer
        self._entries: dict[str, WorkerEntry] = {}

    def add(self, worker_id: str, socket_path: str) -> WorkerEntry:
        entry = WorkerEntry(worker_id, socket_path, added_at=self._timer())
        self._entries[worker_id] = entry
        return entry

    def remove(self, worker_id: str) -> WorkerEntry | None:
        return self._entries.pop(worker_id, None)

    def get(self, worker_id: str) -> WorkerEntry | None:
        return self._entries.get(worker_id)

    def workers(self) -> list[WorkerEntry]:
        return list(self._entries.values())

    def record_status(
        self, worker_id: str, *, pid: int, connections: int, state: str
    ) -> None:
        if (entry := self._entries.get(worker_id)) is None:
            return
        entry.pid = pid
        entry.connections = connections
        if not entry.leaving:
            entry.state = WorkerState(state)
        entry.last_seen = self._timer()

    def unresponsive(self, *, max_silence: float) -> list[WorkerEntry]:
        deadline = self._timer() - max_silence
        return [
            entry
            for entry in self._entries.values()
            if not entry.stopping
            and (entry.added_at if entry.last_seen is None else entry.last_seen)
            < deadline
        ]

    def active_workers(self) -> list[WorkerEntry]:
        return [entry for entry in self._entries.values() if entry.is_ready]

    def drainable(self) -> list[WorkerEntry]:
        return [entry for entry in self._entries.values() if not entry.leaving]

    def has_draining(self) -> bool:
        return any(entry.draining for entry in self._entries.values())

    def total_connections(self) -> int:
        return sum(entry.connections for entry in self._entries.values())
