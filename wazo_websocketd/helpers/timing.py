# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import datetime
import functools
from collections.abc import Iterator
from itertools import chain, repeat


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
