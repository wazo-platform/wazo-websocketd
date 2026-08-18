# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock, patch

from ..worker import Worker, WorkerExit

_CONFIG = {
    'supervised_as': 'worker-1',
    'auth': {},
    'status': {'listen': '127.0.0.1', 'port': 0},
}


class _Server:
    def __init__(self, _config):
        self.started = False

    async def start(self):
        self.started = True

    async def shutdown(self):
        pass

    def connection_count(self):
        return 0


class _NeverReadyServer(_Server):
    """A websocket server whose exchange initialization never completes."""

    async def start(self):
        self.started = True
        await asyncio.Event().wait()  # the bus never connects


def _renewer_that_never_answers():
    renewer = Mock()
    renewer.__aenter__ = AsyncMock(return_value=renewer)
    renewer.__aexit__ = AsyncMock(return_value=False)
    renewer.subscribe = Mock()
    return renewer


@contextmanager
def _worker_running(server, renewer=None):
    with patch('wazo_websocketd.worker.WebsocketServer', return_value=server), patch(
        'wazo_websocketd.worker.ServiceTokenRenewer',
        return_value=renewer or _renewer_that_never_answers(),
    ) as renewer_class, patch('wazo_websocketd.worker.StatusServer') as status_server:
        status_server.return_value.start = AsyncMock()
        status_server.return_value.stop = AsyncMock()
        yield renewer_class


class TestWorkerStartup:
    async def test_a_stop_signal_interrupts_a_blocked_startup(self):
        worker = Worker(_CONFIG)

        with _worker_running(_NeverReadyServer(_CONFIG)):
            running = asyncio.ensure_future(worker.run())
            await asyncio.sleep(0.02)
            worker.request_stop()  # what SIGTERM does

            assert await asyncio.wait_for(running, timeout=2) is WorkerExit.STOPPED

    async def test_a_startup_that_never_finishes_times_out(self):
        worker = Worker(_CONFIG)

        with patch.object(Worker, '_INIT_TIMEOUT', 0.05), _worker_running(
            _NeverReadyServer(_CONFIG)
        ):
            exit_code = await asyncio.wait_for(worker.run(), timeout=2)

        assert exit_code is WorkerExit.INIT_TIMEOUT

    async def test_it_binds_without_waiting_for_the_master_tenant(self):
        worker = Worker(_CONFIG)
        server = _Server(_CONFIG)
        renewer = _renewer_that_never_answers()

        with _worker_running(server, renewer):
            running = asyncio.ensure_future(worker.run())
            await asyncio.sleep(0.02)
            bound = server.started
            worker.request_stop()
            await asyncio.wait_for(running, timeout=2)

        # wazo-auth may not be provisioned yet, and a closed port would fail
        # ExecStartPost and make the service look down
        assert bound is True
        assert renewer.subscribe.call_count == 2  # set the tenant, then retire


class TestMasterTenant:
    async def test_a_supplied_tenant_skips_the_renewer_entirely(self):
        worker = Worker({**_CONFIG, 'master_tenant': 'the-master-tenant'})
        server = _Server(_CONFIG)

        with _worker_running(server) as renewer_class, patch(
            'wazo_websocketd.worker.MasterTenant'
        ) as master_tenant:
            running = asyncio.ensure_future(worker.run())
            await asyncio.sleep(0.02)
            worker.request_stop()
            await asyncio.wait_for(running, timeout=2)

        assert renewer_class.called is False  # no service token needed at all
        assert master_tenant.set_uuid.call_args.args == ('the-master-tenant',)
        assert server.started is True
