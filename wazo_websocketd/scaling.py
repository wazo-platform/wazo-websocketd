# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from os import sched_getaffinity
from typing import Literal

from .helpers.timing import Cooldown
from .registry import WorkerRegistry

logger = logging.getLogger(__name__)

WorkerCount = int | Literal['auto'] | None


def _is_explicit(value: WorkerCount) -> bool:
    return value is not None and value != 'auto'


def _worker_count(value: WorkerCount, key: str, default: int) -> int:
    if value is None or value == 'auto':
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f'configuration key `{key}` must be a positive integer or `auto`'
        )
    return value


def calculate_bounds(config: dict) -> tuple[int, int]:
    ncpu = len(sched_getaffinity(0))
    minimum = config.get('min_workers')
    maximum = config.get('max_workers')
    legacy = config.get('process_workers')

    if legacy is not None and not _is_explicit(minimum) and not _is_explicit(maximum):
        logger.warning(
            'configuration key `process_workers` is deprecated: '
            'use `min_workers` and `max_workers` instead'
        )
        pinned = _worker_count(legacy, 'process_workers', ncpu)
        return pinned, pinned

    floor = _worker_count(minimum, 'min_workers', ncpu)
    ceiling = _worker_count(maximum, 'max_workers', max(floor, ncpu))

    if ceiling < floor:
        if _is_explicit(minimum):
            raise ValueError('`max_workers` must be greater or equal to `min_workers`')
        floor = ceiling

    return floor, ceiling


class ScalingPolicy:
    _HIGH_WATERMARK = 800
    _LOW_WATERMARK = 400
    _SCALE_DOWN_COOLDOWN = 300.0

    def __init__(
        self, config: dict, timer: Callable[[], float] = time.monotonic
    ) -> None:
        self._minimum, self._maximum = calculate_bounds(config)
        self._desired = self._minimum
        self._scale_down_cooldown = Cooldown(self._SCALE_DOWN_COOLDOWN, timer=timer)

    @property
    def desired(self) -> int:
        return self._desired

    def increase(self) -> bool:
        if self._desired >= self._maximum:
            logger.info('already at max_workers (%d)', self._maximum)
            return False

        self._desired += 1
        self._scale_down_cooldown.restart()
        logger.info('scaling up to %d workers (operator nudge)', self._desired)
        return True

    def decrease(self) -> bool:
        if self._desired <= self._minimum:
            logger.info('already at min_workers (%d)', self._minimum)
            return False

        self._desired -= 1
        self._scale_down_cooldown.restart()
        logger.info('scaling down to %d workers (operator nudge)', self._desired)
        return True

    def adjust(self, registry: WorkerRegistry) -> None:
        active = registry.active_workers()
        if not active:
            return

        total = sum(entry.connections for entry in active)
        average = total / len(active)

        if average > self._HIGH_WATERMARK:
            self._scale_down_cooldown.restart()
            self._scale_up(average)
            return

        if average >= self._LOW_WATERMARK:
            self._scale_down_cooldown.restart()
            return

        if not self._scale_down_cooldown.expired:
            return

        if self._desired <= self._minimum:
            return

        if registry.has_draining():
            return

        survivors = len(active) - 1
        if not survivors or total / survivors >= self._HIGH_WATERMARK:
            return

        self._scale_down(average)

    def _scale_up(self, average: float) -> None:
        if self._desired >= self._maximum:
            return

        self._desired += 1
        logger.info(
            'scaling up to %d workers (%.0f connections per worker)',
            self._desired,
            average,
        )

    def _scale_down(self, average: float) -> None:
        self._desired -= 1
        self._scale_down_cooldown.restart()
        logger.info(
            'scaling down to %d workers (%.0f connections per worker)',
            self._desired,
            average,
        )
