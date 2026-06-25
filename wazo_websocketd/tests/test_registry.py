# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import Mock

import pytest

from ..registry import WorkerRegistry


@pytest.fixture
def registry():
    return WorkerRegistry()


def test_select_returns_least_connected_non_draining(registry):
    registry.add('a', Mock())
    registry.add('b', Mock())
    registry.heartbeat('a', connection_count=5)
    registry.heartbeat('b', connection_count=2)

    selected = registry.select()

    assert selected is not None
    assert selected.worker_id == 'b'


def test_select_excludes_draining_workers(registry):
    registry.add('a', Mock())
    registry.add('b', Mock())
    registry.heartbeat('a', connection_count=4)
    registry.heartbeat('b', connection_count=0)
    registry.start_draining('b')

    selected = registry.select()

    assert selected is not None
    assert selected.worker_id == 'a'


def test_select_returns_none_when_no_eligible_worker(registry):
    assert registry.select() is None

    registry.add('a', Mock())
    registry.start_draining('a')

    assert registry.select() is None


def test_note_handoff_increments_count_optimistically(registry):
    entry = registry.add('a', Mock())

    registry.note_handoff(entry)

    assert entry.connection_count == 1


def test_heartbeat_updates_count_and_freshness(registry):
    registry.add('a', Mock(), now=0.0)

    registry.heartbeat('a', connection_count=7, now=100.0)

    entry = registry.get('a')
    assert entry is not None
    assert entry.connection_count == 7
    assert entry.last_heartbeat == 100.0


def test_heartbeat_for_unknown_worker_is_ignored(registry):
    registry.heartbeat('ghost', connection_count=1)  # must not raise

    assert registry.get('ghost') is None


def test_unresponsive_workers_identifies_without_removing(registry):
    registry.add('a', Mock(), now=0.0)
    registry.add('b', Mock(), now=0.0)
    registry.heartbeat('b', connection_count=0, now=90.0)

    unresponsive = registry.unresponsive_workers(max_silence=30.0, now=100.0)

    assert unresponsive == ['a']
    assert registry.get('a') is not None  # identifying does not remove
    assert registry.get('b') is not None


def test_remove_returns_and_deregisters_worker(registry):
    entry = registry.add('a', Mock())

    removed = registry.remove('a')

    assert removed is entry
    assert registry.get('a') is None
    assert registry.select() is None
    assert registry.remove('a') is None
