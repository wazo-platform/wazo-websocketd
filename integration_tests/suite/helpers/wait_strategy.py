# Copyright 2022-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import time
import asyncio

from websockets import ConnectionClosed
from websockets.exceptions import InvalidMessage

from .constants import START_TIMEOUT
from .websocketd import WebSocketdClient, WebSocketdTimeoutError


class WaitStrategy:
    def wait(self, integration_test):
        raise NotImplementedError


class TimeWaitStrategy(WaitStrategy):
    def wait(self, integration_test):
        time.sleep(10)


class WaitUntilValidConnection(WaitStrategy):
    timeout = START_TIMEOUT

    def wait(self, test):
        loop = asyncio.get_event_loop()
        fut = asyncio.ensure_future(self.await_for_connection(test))
        if not loop.is_running():
            loop.run_until_complete(fut)

    async def await_for_connection(self, test):
        port = test.service_port(9502, 'websocketd')
        client = WebSocketdClient(port)

        with test.auth_client.token() as token:
            for _ in range(self.timeout):
                try:
                    await client.connect_and_wait_for_init(token)
                except (ConnectionClosed, AssertionError, InvalidMessage):
                    await asyncio.sleep(1)
                else:
                    return
                finally:
                    await client.close()
            raise WebSocketdTimeoutError
