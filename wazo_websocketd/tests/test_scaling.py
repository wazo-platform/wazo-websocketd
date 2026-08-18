# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
from unittest.mock import patch

import pytest

from ..registry import WorkerRegistry
from ..scaling import ScalingPolicy, calculate_bounds
from .helpers import Clock

_BOUNDS = {'min_workers': 2, 'max_workers': 4}


def _ncpu(count):
    return patch(
        'wazo_websocketd.scaling.sched_getaffinity', return_value=set(range(count))
    )


def _policy(clock=None, config=None):
    return ScalingPolicy(config or _BOUNDS, timer=clock or Clock())


def _watermarks(high, low):
    return patch.multiple(ScalingPolicy, _HIGH_WATERMARK=high, _LOW_WATERMARK=low)


def _pool(*connections, draining=()):
    registry = WorkerRegistry()
    for index, count in enumerate(connections, start=1):
        worker_id = f'worker-{index}'
        registry.add(worker_id, f'/run/x/{worker_id}.sock')
        state = 'draining' if worker_id in draining else 'ready'
        registry.record_status(worker_id, pid=index, connections=count, state=state)
    return registry


class TestBounds:
    def test_defaults_resolve_to_a_fixed_pool_of_ncpu(self):
        with _ncpu(8):
            assert calculate_bounds({}) == (8, 8)

    def test_explicit_bounds_are_honored(self):
        assert calculate_bounds({'min_workers': 2, 'max_workers': 6}) == (2, 6)

    def test_auto_max_never_goes_below_min(self):
        with _ncpu(4):
            assert calculate_bounds({'min_workers': 16}) == (16, 16)

    def test_a_max_alone_also_lowers_the_defaulted_min(self):
        with _ncpu(16):
            assert calculate_bounds({'max_workers': 4}) == (4, 4)
            assert calculate_bounds({'max_workers': 4, 'min_workers': 'auto'}) == (4, 4)

    def test_a_max_alone_does_not_lower_a_smaller_default(self):
        with _ncpu(2):
            assert calculate_bounds({'max_workers': 4}) == (2, 4)

    def test_invalid_bounds_are_rejected(self):
        with pytest.raises(ValueError):
            calculate_bounds({'min_workers': 0})
        with pytest.raises(ValueError):
            calculate_bounds({'min_workers': True})
        with pytest.raises(ValueError):
            calculate_bounds({'min_workers': 4, 'max_workers': 2})
        with pytest.raises(ValueError):
            calculate_bounds({'min_workers': 'many'})


class TestLegacyProcessWorkers:
    def test_legacy_process_workers_pins_the_pool(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert calculate_bounds({'process_workers': 3}) == (3, 3)
        assert any('deprecated' in message for message in caplog.messages)

    def test_legacy_auto_process_workers_resolves_to_ncpu(self):
        with _ncpu(8):
            assert calculate_bounds({'process_workers': 'auto'}) == (8, 8)

    def test_explicit_bounds_win_over_legacy_process_workers(self):
        assert calculate_bounds(
            {'process_workers': 3, 'min_workers': 2, 'max_workers': 5}
        ) == (
            2,
            5,
        )


class TestScalingUp:
    def test_the_pool_starts_at_min_workers(self):
        assert _policy().desired == 2

    def test_breaching_the_high_watermark_asks_for_one_more_worker(self):
        policy = _policy()

        policy.adjust(_pool(900, 900))
        assert policy.desired == 3

    def test_scaling_up_stops_at_max_workers(self):
        policy = _policy(config={'min_workers': 2, 'max_workers': 3})

        policy.adjust(_pool(900, 900))
        policy.adjust(_pool(900, 900, 900))
        assert policy.desired == 3

    def test_an_average_inside_the_band_changes_nothing(self):
        policy = _policy()

        policy.adjust(_pool(500, 500))
        assert policy.desired == 2

    def test_an_empty_pool_is_left_alone(self):
        policy = _policy()

        policy.adjust(_pool())
        assert policy.desired == 2


class TestScalingDown:
    def test_dropping_below_the_low_watermark_waits_for_the_cooldown(self):
        clock = Clock()
        policy = _policy(clock)
        policy.adjust(_pool(900, 900))  # load pushed the pool to 3

        policy.adjust(_pool(100, 100, 100))  # cooldown starts here
        assert policy.desired == 3

        clock.now += 299.0
        policy.adjust(_pool(100, 100, 100))
        assert policy.desired == 3

        clock.now += 2.0
        policy.adjust(_pool(100, 100, 100))
        assert policy.desired == 2

    def test_returning_inside_the_band_restarts_the_cooldown(self):
        clock = Clock()
        policy = _policy(clock)
        policy.adjust(_pool(900, 900))

        policy.adjust(_pool(100, 100, 100))
        clock.now += 299.0
        policy.adjust(_pool(500, 500, 500))  # back in band

        clock.now += 2.0
        policy.adjust(_pool(100, 100, 100))
        assert policy.desired == 3

    def test_no_scaling_down_below_min_workers(self):
        clock = Clock()
        policy = _policy(clock)

        policy.adjust(_pool(100, 100))
        clock.now += 301.0
        policy.adjust(_pool(100, 100))
        assert policy.desired == 2

    def test_no_scaling_down_that_would_overload_the_survivors(self):
        clock = Clock()
        policy = _policy(clock)

        # unreachable with the shipped watermarks: an average below half the high
        # one always leaves the survivors under it
        with _watermarks(high=100, low=90):
            policy.adjust(_pool(200, 200))
            policy.adjust(_pool(80, 80))
            clock.now += 301.0
            policy.adjust(_pool(80, 80))

        assert policy.desired == 3

    def test_no_scaling_down_while_a_worker_is_already_draining(self):
        clock = Clock()
        policy = _policy(clock)
        policy.adjust(_pool(900, 900))

        draining = _pool(100, 100, 100, draining=('worker-3',))
        policy.adjust(draining)
        clock.now += 301.0
        policy.adjust(draining)
        assert policy.desired == 3


class TestOperatorNudges:
    def test_an_operator_can_increase_and_decrease_within_bounds(self):
        policy = _policy()

        assert policy.increase() is True
        assert policy.increase() is True
        assert policy.desired == 4
        assert policy.increase() is False  # at max_workers
        assert policy.desired == 4

        assert policy.decrease() is True
        assert policy.decrease() is True
        assert policy.desired == 2
        assert policy.decrease() is False  # at min_workers
        assert policy.desired == 2

    def test_an_operator_nudge_up_restarts_the_scale_down_cooldown(self):
        clock = Clock()
        policy = _policy(clock)
        policy.adjust(_pool(100, 100))  # idle: the cooldown starts, but we sit at min
        clock.now += 301.0
        policy.adjust(_pool(100, 100))

        policy.increase()
        policy.adjust(_pool(100, 100, 100))

        assert policy.desired == 3

    def test_an_operator_nudge_down_restarts_the_scale_down_cooldown(self):
        clock = Clock()
        policy = _policy(clock)
        for _ in range(2):
            policy.increase()
        policy.adjust(_pool(100, 100, 100, 100))
        clock.now += 301.0

        policy.decrease()
        policy.adjust(_pool(100, 100, 100))

        assert policy.desired == 3
