# Copyright 2022-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

import aiohttp
from wazo_test_helpers.asset_launching_test_case import NoSuchPort, NoSuchService
from wazo_test_helpers.wait_strategy import WaitStrategy
from websockets import ConnectionClosed
from websockets.exceptions import InvalidMessage

from .constants import START_TIMEOUT
from .websocketd import WebSocketdClient, WebSocketdTimeoutError

if TYPE_CHECKING:
    from .base import _BaseAssetLaunchingTestCase

    Asset = type[_BaseAssetLaunchingTestCase]


class ReadinessCheck(Protocol):
    poll_interval: float

    async def is_ready(self, test: Asset) -> bool:
        ...


class AsyncWaitStrategy(WaitStrategy):
    def __init__(self, *checks: ReadinessCheck, timeout: float = START_TIMEOUT) -> None:
        self._checks = checks
        self._timeout = timeout

    def wait(self, test: Asset) -> None:
        asyncio.run(self.await_ready(test))

    async def await_ready(self, test: Asset) -> None:
        await asyncio.gather(*[self._poll(check, test) for check in self._checks])

    async def _poll(self, check: ReadinessCheck, test: Asset) -> None:
        for _ in range(int(self._timeout / check.poll_interval)):
            if await check.is_ready(test):
                return
            await asyncio.sleep(check.poll_interval)
        raise WebSocketdTimeoutError(f'{type(check).__name__} timed out')


class BrokerReady:
    poll_interval = 1.0

    async def is_ready(self, test: Asset) -> bool:
        try:
            test.service_port(9504, 'broker')
        except (NoSuchService, NoSuchPort):
            return True  # this asset runs without a broker

        try:
            status, _ = await test.broker_status(timeout=self.poll_interval)
        except (aiohttp.ClientError, OSError):
            return False
        return status == 200


class WebsocketdReady:
    poll_interval = 0.25

    async def is_ready(self, test: Asset) -> bool:
        client = WebSocketdClient(test.service_port(9502, 'websocketd'))
        with test.auth_client.token() as token:
            try:
                await client.connect_and_wait_for_init(token)
            except (ConnectionClosed, AssertionError, InvalidMessage, OSError):
                return False
            else:
                return True
            finally:
                await client.close()
