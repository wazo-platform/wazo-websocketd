# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import datetime
import functools
import time
from collections.abc import Callable, Iterator
from itertools import chain, repeat

from wazo_websocketd.exception import CrashBudgetExhausted


def utcnow_naive() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


@functools.lru_cache(maxsize=4096)
def parse_expiration(value: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.UTC).replace(tzinfo=None)
    return parsed


def exponential_backoff(delay: float, retries: int) -> Iterator[float]:
    for attempt in range(retries):
        yield delay * 2**attempt


def capped_backoff(delay: float, retries: int, ceiling: float) -> Iterator[float]:
    return chain(exponential_backoff(delay, retries), repeat(ceiling))


class Cooldown:
    def __init__(
        self, duration: float, timer: Callable[[], float] = time.monotonic
    ) -> None:
        self._duration = duration
        self._timer = timer
        self._since = timer()

    @property
    def elapsed(self) -> float:
        return self._timer() - self._since

    @property
    def expired(self) -> bool:
        return self.elapsed >= self._duration

    def restart(self) -> None:
        self._since = self._timer()


class CrashBucket:
    def __init__(
        self,
        *,
        burst: int,
        window: float,
        timer: Callable[[], float] = time.monotonic,
    ) -> None:
        self._burst = burst
        self._window = window
        self._timer = timer
        self._crashes: list[float] = []

    def record(self) -> None:
        now = self._timer()
        self._crashes = [at for at in self._crashes if at > now - self._window]
        self._crashes.append(now)
        if len(self._crashes) > self._burst:
            raise CrashBudgetExhausted(
                f'{len(self._crashes)} crashes in {self._window:.0f}s'
            )
