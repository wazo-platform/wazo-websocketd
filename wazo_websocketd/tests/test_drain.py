# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio

import pytest
import websockets

from ..worker import drain
from .helpers import echo_handler


class TestDrain:
    @pytest.fixture(autouse=True)
    async def server(self):
        self.server = await websockets.serve(echo_handler, host='127.0.0.1', port=0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.clients: list[websockets.WebSocketClientProtocol] = []
        yield
        for client in self.clients:
            await client.close()

    async def _connect(self, count=1):
        for _ in range(count):
            self.clients.append(
                await websockets.connect(f'ws://127.0.0.1:{self.port}/')
            )
        return self.clients

    async def test_it_closes_open_connections_as_going_away(self):
        (client,) = await self._connect()

        await drain(self.server)

        with pytest.raises(websockets.ConnectionClosed) as closed:
            await asyncio.wait_for(client.recv(), timeout=2)
        assert closed.value.code == 1001

    async def test_it_stops_accepting_new_connections(self):
        await drain(self.server)

        with pytest.raises(OSError):
            await asyncio.wait_for(
                websockets.connect(f'ws://127.0.0.1:{self.port}/'), timeout=1
            )

    async def test_it_does_not_wait_for_clients_to_disconnect(self):
        await self._connect()  # left open on purpose

        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(drain(self.server), timeout=2)

        # sessions only end when the client goes away, so waiting never helps
        assert loop.time() - started < 1

    async def test_it_spreads_the_closes_over_the_window(self):
        clients = await self._connect(6)
        loop = asyncio.get_running_loop()
        closed_at = []

        async def watch(client):
            try:
                await client.recv()
            except websockets.ConnectionClosed:
                closed_at.append(loop.time())

        watchers = [asyncio.create_task(watch(client)) for client in clients]
        await drain(self.server, window=0.6)
        await asyncio.gather(*watchers)

        # the herd reconnects as a ramp rather than all at once
        assert max(closed_at) - min(closed_at) > 0.3
