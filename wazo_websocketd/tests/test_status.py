# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import os
from unittest.mock import patch

import aiohttp
import pytest

from .. import status as status_module
from ..status import StatusClient, StatusServer, socket_peer_pid


def _server(
    supervised,
    connections=0,
    name='worker-1',
    ready=True,
    listen=status_module.STATUS_LISTEN,
    port=0,
):
    return StatusServer(
        name=name,
        supervised=supervised,
        connection_counter=lambda: connections,
        ready_checker=lambda: ready,
        listen=listen,
        port=port,
    )


async def _get_status(url, connector=None):
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(url) as response:
            return response.status, await response.json()


class TestStatusOverTcp:
    @pytest.fixture(autouse=True)
    async def stopped_afterwards(self):
        self.servers: list[StatusServer] = []
        yield
        for server in self.servers:
            await server.stop()

    async def _started(self, **kwargs):
        server = _server(False, **kwargs)
        self.servers.append(server)
        await server.start()
        return server

    async def test_a_ready_process_answers_200_with_its_own_identity(self):
        server = await self._started(connections=42)

        status, body = await _get_status(f'http://127.0.0.1:{server.port}/status')

        assert status == 200
        assert body == {
            'state': 'ready',
            'connections': 42,
            'name': 'worker-1',
            'pid': os.getpid(),
        }

    async def test_a_draining_process_answers_503_and_says_so(self):
        server = await self._started(connections=7)
        server.set_draining()

        status, body = await _get_status(f'http://127.0.0.1:{server.port}/status')

        assert status == 503
        assert body['state'] == 'draining'
        assert body['connections'] == 7

    async def test_a_process_that_is_not_ready_yet_answers_503(self):
        server = await self._started(ready=False)

        status, body = await _get_status(f'http://127.0.0.1:{server.port}/status')

        assert status == 503
        assert body['state'] == 'starting'

    async def test_it_listens_where_it_is_told(self):
        server = await self._started(listen='127.0.0.1')

        host, port = server._runner.addresses[0][:2]
        status, _ = await _get_status(f'http://{host}:{port}/status')

        assert host == '127.0.0.1'
        assert port != status_module.STATUS_PORT
        assert status == 200


class TestStatusOverUnixSocket:
    @pytest.fixture(autouse=True)
    async def run_dir(self, tmp_path):
        self.servers: list[StatusServer] = []
        with patch.object(status_module, 'RUN_DIR', str(tmp_path)):
            self.run_dir = tmp_path
            yield
        for server in self.servers:
            await server.stop()

    async def _started(self, name):
        server = _server(True, name=name)
        self.servers.append(server)
        await server.start()
        return server, server.socket_path

    async def test_a_supervised_process_serves_on_a_socket_named_after_it(self):
        server, socket_path = await self._started('worker-7')

        connector = aiohttp.UnixConnector(path=socket_path)
        status, body = await _get_status('http://localhost/status', connector)

        assert socket_path == str(self.run_dir / 'worker-7.sock')
        assert status == 200
        assert body['name'] == 'worker-7'

    async def test_the_socket_is_removed_when_the_process_stops(self):
        server, socket_path = await self._started('worker-7')

        await server.stop()

        assert os.path.exists(socket_path) is False


class TestStatusClient:
    @pytest.fixture(autouse=True)
    async def run_dir(self, tmp_path):
        self.servers: list[StatusServer] = []
        self.client = StatusClient(idle_timeout=60.0)
        with patch.object(status_module, 'RUN_DIR', str(tmp_path)):
            yield
        await self.client.close()
        for server in self.servers:
            await server.stop()

    async def _serving(self, name, connections=0):
        server = _server(True, name=name, connections=connections)
        self.servers.append(server)
        await server.start()
        return server.socket_path

    async def test_it_reads_the_status_a_worker_serves(self):
        socket_path = await self._serving('worker-3', connections=5)

        status = await self.client.fetch(socket_path)

        assert status == {
            'name': 'worker-3',
            'connections': 5,
            'state': 'ready',
            'pid': os.getpid(),
        }

    async def test_it_reads_a_draining_worker_too(self):
        socket_path = await self._serving('worker-3')
        self.servers[0].set_draining()

        status = await self.client.fetch(socket_path)

        assert status is not None and status['state'] == 'draining'

    async def test_a_socket_nobody_serves_reads_as_nothing(self, tmp_path):
        assert await self.client.fetch(str(tmp_path / 'nobody-home.sock')) is None

    async def test_it_closes_the_session_of_a_socket_it_is_told_to_forget(self):
        socket_path = await self._serving('worker-3')
        await self.client.fetch(socket_path)
        session = self.client._sockets[socket_path].session

        await self.client.forget(socket_path)

        assert set(self.client._sockets) == set()
        assert session.closed is True

    async def test_it_closes_the_session_of_a_socket_left_unpolled(self):
        gone = await self._serving('worker-3')
        kept = await self._serving('worker-4')
        client = StatusClient(idle_timeout=0.05)
        try:
            await client.fetch(gone)
            await client.fetch(kept)
            session = client._sockets[gone].session

            # nobody told us worker-3 left, so going unpolled has to be enough
            await asyncio.sleep(0.06)
            await client.fetch(kept)

            assert set(client._sockets) == {kept}
            assert session.closed is True
        finally:
            await client.close()


class TestSocketPeerPid:
    async def test_it_names_the_process_listening_on_the_socket(self, tmp_path):
        with patch.object(status_module, 'RUN_DIR', str(tmp_path)):
            server = _server(True, name='worker-5')
            await server.start()
            socket_path = server.socket_path
        try:
            assert await socket_peer_pid(socket_path) == os.getpid()
        finally:
            await server.stop()

    async def test_it_is_none_when_nobody_listens(self, tmp_path):
        assert await socket_peer_pid(str(tmp_path / 'nobody.sock')) is None
