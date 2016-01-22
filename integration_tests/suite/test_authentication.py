# Copyright 2016 by Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import unittest

from .test_api.base import IntegrationTest
from .test_api.constants import INVALID_TOKEN, VALID_TOKEN


@asyncio.coroutine
def connect_and_wait_for_close(websocketd_client, token_id):
    yield from websocketd_client.connect(token_id)
    try:
        yield from websocketd_client.wait_for_close()
    finally:
        yield from websocketd_client.close()


@asyncio.coroutine
def connect_and_wait_for_nothing(websocketd_client, token_id):
    yield from websocketd_client.connect(token_id)
    try:
        yield from websocketd_client.wait_for_nothing()
    finally:
        yield from websocketd_client.close()


class TestAuthentication(IntegrationTest):

    asset = 'basic'

    def test_invalid_auth_closes_websocket(self):
        coro = connect_and_wait_for_close(self.websocketd_client, INVALID_TOKEN)
        self.loop.run_until_complete(coro)
 
    def test_valid_auth_gives_result(self):
        coro = connect_and_wait_for_nothing(self.websocketd_client, VALID_TOKEN)
        self.loop.run_until_complete(coro)


class TestAuthenticationError(IntegrationTest):

    asset = 'no_auth_server'

    @unittest.expectedFailure
    def test_no_auth_server_closes_websocket(self):
        coro = connect_and_wait_for_close(self.websocketd_client, VALID_TOKEN)
        self.loop.run_until_complete(coro)
