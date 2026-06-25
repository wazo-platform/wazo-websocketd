# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass
from multiprocessing.context import SpawnProcess

logger = logging.getLogger(__name__)


@dataclass
class WorkerEntry:
    worker_id: str
    control_sock: socket.socket
    connection_count: int = 0
    draining: bool = False
    last_heartbeat: float = 0.0
    process: SpawnProcess | None = None
    heartbeat_task: asyncio.Task | None = None


class WorkerRegistry:
    def __init__(self) -> None:
        self._workers: dict[str, WorkerEntry] = {}

    def add(
        self, worker_id: str, control_sock: socket.socket, *, now: float | None = None
    ) -> WorkerEntry:
        entry = WorkerEntry(
            worker_id,
            control_sock,
            last_heartbeat=now if now is not None else time.monotonic(),
        )
        self._workers[worker_id] = entry
        return entry

    def remove(self, worker_id: str) -> WorkerEntry | None:
        return self._workers.pop(worker_id, None)

    def get(self, worker_id: str) -> WorkerEntry | None:
        return self._workers.get(worker_id)

    def workers(self) -> list[WorkerEntry]:
        return list(self._workers.values())

    def heartbeat(
        self, worker_id: str, connection_count: int, *, now: float | None = None
    ) -> None:
        entry = self._workers.get(worker_id)
        if entry is None:
            return
        entry.connection_count = connection_count
        entry.last_heartbeat = now if now is not None else time.monotonic()

    def start_draining(self, worker_id: str) -> None:
        entry = self._workers.get(worker_id)
        if entry is not None:
            entry.draining = True

    def note_handoff(self, entry: WorkerEntry) -> None:
        # Optimistic bump between heartbeats; the next heartbeat reconciles it.
        entry.connection_count += 1

    def select(self) -> WorkerEntry | None:
        candidates = [w for w in self._workers.values() if not w.draining]
        if not candidates:
            return None
        return min(candidates, key=lambda w: w.connection_count)

    def unresponsive_workers(
        self, max_silence: float, *, now: float | None = None
    ) -> list[str]:
        now = now if now is not None else time.monotonic()
        return [
            worker_id
            for worker_id, entry in self._workers.items()
            if now - entry.last_heartbeat > max_silence
        ]
