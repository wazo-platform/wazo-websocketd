# Copyright 2022-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio

from websockets import ConnectionClosed
from websockets.exceptions import InvalidMessage

from .constants import START_TIMEOUT
from .websocketd import WebSocketdClient, WebSocketdTimeoutError


class WaitStrategy:
    def wait(self, integration_test):
        raise NotImplementedError


class WaitUntilValidConnection(WaitStrategy):
    _POLL_INTERVAL = 0.25

    timeout = START_TIMEOUT

    def wait(self, test):
        asyncio.run(self.await_for_connection(test))

    async def await_for_connection(self, test):
        port = test.service_port(9502, 'websocketd')
        client = WebSocketdClient(port)

        with test.auth_client.token() as token:
            for _ in range(int(self.timeout / self._POLL_INTERVAL)):
                try:
                    await client.connect_and_wait_for_init(token)
                except (ConnectionClosed, AssertionError, InvalidMessage, OSError):
                    await asyncio.sleep(self._POLL_INTERVAL)
                else:
                    return
                finally:
                    await client.close()
            raise WebSocketdTimeoutError
