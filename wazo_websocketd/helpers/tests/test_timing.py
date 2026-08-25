# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import datetime
from itertools import chain, islice, repeat

from ...tests.helpers import Clock
from ..timing import Cooldown, exponential_backoff, parse_expiration, utcnow_naive


class TestExponentialBackoff:
    def test_each_delay_doubles_the_last(self):
        assert list(exponential_backoff(0.05, 4)) == [0.05, 0.1, 0.2, 0.4]

    def test_no_retries_yields_nothing(self):
        assert list(exponential_backoff(1, 0)) == []

    def test_it_composes_with_a_ceiling(self):
        delays = chain(exponential_backoff(1, 5), repeat(32))

        assert list(islice(delays, 7)) == [1, 2, 4, 8, 16, 32, 32]


class TestExpiration:
    def test_a_naive_timestamp_is_taken_as_it_is(self):
        assert parse_expiration('2026-01-01T00:00:00') == datetime.datetime(
            2026, 1, 1, 0, 0, 0
        )

    def test_an_aware_timestamp_is_converted_to_naive_utc(self):
        assert parse_expiration('2026-01-01T05:00:00+05:00') == datetime.datetime(
            2026, 1, 1, 0, 0, 0
        )

    def test_utcnow_is_naive(self):
        assert utcnow_naive().tzinfo is None


class TestCooldown:
    def test_the_wait_starts_when_the_cooldown_does(self):
        clock = Clock()
        cooldown = Cooldown(10.0, timer=clock)

        assert cooldown.expired is False

        clock.now += 10.0
        assert cooldown.expired is True

    def test_looking_does_not_extend_the_wait(self):
        clock = Clock()
        cooldown = Cooldown(10.0, timer=clock)

        expired = []
        for _ in range(3):
            expired.append(cooldown.expired)
            clock.now += 5.0

        assert expired == [False, False, True]

    def test_restarting_puts_the_full_wait_back(self):
        clock = Clock()
        cooldown = Cooldown(10.0, timer=clock)

        clock.now += 9.0
        cooldown.restart()

        clock.now += 9.0
        assert cooldown.expired is False

        clock.now += 1.0
        assert cooldown.expired is True

    def test_a_cooldown_reports_how_long_it_has_been_waiting(self):
        clock = Clock()
        cooldown = Cooldown(10.0, timer=clock)

        assert cooldown.elapsed == 0.0

        clock.now += 4.0
        assert cooldown.elapsed == 4.0

        cooldown.restart()
        assert cooldown.elapsed == 0.0
