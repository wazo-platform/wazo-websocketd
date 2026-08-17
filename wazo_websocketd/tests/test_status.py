# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import os
from unittest.mock import patch

import aiohttp
import pytest

from .. import status as status_module
from ..status import StatusServer, socket_peer_pid


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
