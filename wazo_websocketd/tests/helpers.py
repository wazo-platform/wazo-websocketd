# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from aiohttp import web

from ..auth import AsyncAuthClient, build_auth_client


async def echo_handler(ws, path: str) -> None:
    async for message in ws:
        await ws.send(message)


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeWazoAuth:
    def __init__(self, socket_path: str | None = None) -> None:
        self.tokens: dict[str, dict[str, Any]] = {}
        self.statuses: dict[str, int] = {}
        self.requests: list[tuple[str, str, str | None]] = []
        self.created: list[tuple[dict[str, Any], str | None]] = []
        self.created_token: dict[str, Any] = {'token': 'service-token'}
        self._socket_path = socket_path
        self._runner: web.AppRunner = None  # type: ignore[assignment]

    @property
    def port(self) -> int:
        return self._runner.addresses[0][1]

    def config(self) -> dict[str, Any]:
        if self._socket_path:
            return {'host': self._socket_path, 'https': False}
        return {'host': '127.0.0.1', 'port': self.port, 'https': False}

    def broker_config(self) -> dict[str, Any]:
        listen = self._socket_path or '127.0.0.1'
        port = None if self._socket_path else self.port
        return {'listen': listen, 'port': port, 'https': False}

    async def start(self) -> None:
        app = web.Application()
        app.add_routes(
            [
                web.head('/0.1/token/{token_id}', self._handle),
                web.get('/0.1/token/{token_id}', self._handle, allow_head=False),
                web.post('/0.1/token', self._create),
            ]
        )
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        if self._socket_path:
            await web.UnixSite(self._runner, self._socket_path).start()
        else:
            await web.TCPSite(self._runner, '127.0.0.1', 0).start()

    async def stop(self) -> None:
        await self._runner.cleanup()

    async def _create(self, request: web.Request) -> web.Response:
        self.created.append(
            (await request.json(), request.headers.get('Authorization'))
        )
        return web.json_response({'data': self.created_token})

    async def _handle(self, request: web.Request) -> web.Response:
        token_id = request.match_info['token_id']
        self.requests.append((request.method, token_id, request.query.get('scope')))
        status = self.statuses.get(token_id, 200 if token_id in self.tokens else 404)
        if status != 200:
            if request.method == 'HEAD':
                return web.Response(status=status)
            return web.json_response({'reason': ['forced']}, status=status)
        if request.method == 'HEAD':
            return web.Response(status=204)
        return web.json_response({'data': self.tokens[token_id]})


@asynccontextmanager
async def fake_wazo_auth(socket_path: str | None = None) -> AsyncIterator[FakeWazoAuth]:
    fake = FakeWazoAuth(socket_path)
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@asynccontextmanager
async def auth_client(config: dict[str, Any]) -> AsyncIterator[AsyncAuthClient]:
    client = AsyncAuthClient(config['auth'])
    try:
        yield client
    finally:
        await client.close()


@asynccontextmanager
async def broker_client(config: dict[str, Any]) -> AsyncIterator[AsyncAuthClient]:
    client = build_auth_client(config)
    try:
        yield client
    finally:
        await client.close()


def _closed_port() -> int:
    with socket.socket() as free_port_probe:
        free_port_probe.bind(('127.0.0.1', 0))
        return free_port_probe.getsockname()[1]


def refused_config() -> dict[str, Any]:
    return {'host': '127.0.0.1', 'port': _closed_port(), 'https': False}


def refused_broker_config() -> dict[str, Any]:
    return {'listen': '127.0.0.1', 'port': _closed_port(), 'https': False}
