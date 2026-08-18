# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from signal import SIGKILL, SIGTERM

import pytest

from wazo_websocketd.tests.helpers import spawn_sleeper

from ..process import (
    child_command,
    is_pid_running,
    signal_pid,
    signal_process,
    stop_children,
    stop_pids,
)


class TestLiveness:
    async def test_a_live_pid_is_running(self):
        process = await spawn_sleeper()
        try:
            assert is_pid_running(process.pid) is True
        finally:
            process.kill()
            await process.wait()

    async def test_a_reaped_pid_is_not_running(self):
        process = await spawn_sleeper()
        process.kill()
        await process.wait()

        assert is_pid_running(process.pid) is False


class TestSignalling:
    async def test_signalling_a_pid_stops_that_process(self):
        process = await spawn_sleeper()

        signal_pid(process.pid)

        assert await asyncio.wait_for(process.wait(), timeout=5) == -SIGTERM

    async def test_signalling_a_pid_nobody_runs_is_ignored(self):
        process = await spawn_sleeper()
        process.kill()
        await process.wait()

        signal_pid(process.pid, SIGKILL)  # a reused pid must not be killed either

    async def test_signalling_a_child_relays_the_requested_signal(self):
        process = await spawn_sleeper()

        signal_process(process, SIGKILL)

        assert await asyncio.wait_for(process.wait(), timeout=5) == -SIGKILL

    async def test_signalling_a_child_that_already_exited_is_ignored(self):
        process = await spawn_sleeper()
        process.kill()
        exit_code = await process.wait()

        signal_process(process)

        assert exit_code == -SIGKILL


class TestStopping:
    async def test_a_child_that_ignores_sigterm_is_killed(self):
        process = await spawn_sleeper()

        await stop_children([process], grace=0.05, label='sleeper')

        assert process.returncode == -SIGKILL

    async def test_a_pid_that_ignores_sigterm_is_killed(self):
        process = await spawn_sleeper()

        await stop_pids([process.pid], grace=0.05, interval=0.01, label='sleeper')

        assert await asyncio.wait_for(process.wait(), timeout=5) == -SIGKILL

    async def test_stopping_nothing_is_not_an_error(self):
        await stop_children([], grace=0.05, label='nobody')
        await stop_pids([], grace=0.05, interval=0.01, label='nobody')


class TestChildCommand:
    @pytest.mark.parametrize(
        ('config', 'expected'),
        [
            pytest.param({}, [], id='nothing to pass on'),
            pytest.param(
                {'config_file': '/etc/wazo-websocketd/config.yml'},
                ['-c', '/etc/wazo-websocketd/config.yml'],
                id='the config file it was given',
            ),
            pytest.param({'debug': True}, ['-d'], id='debug'),
        ],
    )
    def test_a_child_inherits_what_it_needs_to_be_told(self, config, expected):
        command = child_command(config, 'wazo-websocketd-worker', 'worker-1')

        assert command[0] == 'wazo-websocketd-worker'
        assert command[1:3] == ['--supervised-as', 'worker-1']
        assert command[3:] == expected
