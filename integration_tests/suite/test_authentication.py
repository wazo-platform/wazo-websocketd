# Copyright 2016-2020 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import websockets

from .helpers.base import IntegrationTest, run_with_loop
from .helpers.constants import (
    INVALID_TOKEN_ID,
    UNAUTHORIZED_TOKEN_ID,
    CLOSE_CODE_AUTH_FAILED,
    CLOSE_CODE_AUTH_EXPIRED,
    CLOSE_CODE_NO_TOKEN_ID,
)


class TestAuthentication(IntegrationTest):

    asset = 'basic'

    @run_with_loop
    async def test_no_token_closes_websocket(self):
        await self.websocketd_client.connect_and_wait_for_close(
            None, CLOSE_CODE_NO_TOKEN_ID
        )

    @run_with_loop
    async def test_valid_auth_gives_result(self):
        await self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)

    @run_with_loop
    async def test_invalid_auth_closes_websocket(self):
        await self.websocketd_client.connect_and_wait_for_close(
            INVALID_TOKEN_ID, CLOSE_CODE_AUTH_FAILED
        )

    @run_with_loop
    async def test_unauthorized_auth_closes_websocket(self):
        await self.websocketd_client.connect_and_wait_for_close(
            UNAUTHORIZED_TOKEN_ID, CLOSE_CODE_AUTH_FAILED
        )


class TestNoXivoAuth(IntegrationTest):

    asset = 'no_auth_server'

    @run_with_loop
    async def test_no_auth_server_closes_websocket(self):
        await self.websocketd_client.connect_and_wait_for_close(self.valid_token_id)


class TestTokenExpiration(IntegrationTest):

    asset = 'token_expiration'

    _TIMEOUT = 15

    def setUp(self):
        super().setUp()
        self.token_id = 'dynamic-token'

    @run_with_loop
    async def test_token_expire_closes_websocket(self):
        self.auth_server.put_token(self.token_id)

        await self.websocketd_client.connect_and_wait_for_init(self.token_id)
        self.auth_server.remove_token(self.token_id)
        self.websocketd_client.timeout = self._TIMEOUT
        await self.websocketd_client.wait_for_close(CLOSE_CODE_AUTH_EXPIRED)

    @run_with_loop
    async def test_token_renew(self):
        self.auth_server.put_token(self.token_id)

        await self.websocketd_client.connect_and_wait_for_init(self.token_id, version=2)
        await self.websocketd_client.op_start()
        self.auth_server.put_token("new-token")
        await self.websocketd_client.op_token("new-token")
        self.auth_server.remove_token(self.token_id)
        self.websocketd_client.timeout = self._TIMEOUT
        await self.websocketd_client.wait_for_nothing()

    @run_with_loop
    async def test_token_renew_and_expire(self):
        self.auth_server.put_token(self.token_id)

        await self.websocketd_client.connect_and_wait_for_init(self.token_id, version=2)
        await self.websocketd_client.op_start()
        self.auth_server.put_token("new-token")
        await self.websocketd_client.op_token("new-token")
        self.websocketd_client.timeout = self._TIMEOUT
        await self.websocketd_client.wait_for_nothing()
        self.auth_server.remove_token("new-token")
        self.websocketd_client.timeout = self._TIMEOUT
        await self.websocketd_client.wait_for_close(CLOSE_CODE_AUTH_EXPIRED)

    @run_with_loop
    async def test_token_renew_invalid(self):
        self.auth_server.put_token(self.token_id)

        await self.websocketd_client.connect_and_wait_for_init(self.token_id, version=2)
        await self.websocketd_client.op_start()
        try:
            await self.websocketd_client.op_token("invalid-token")
        except websockets.ConnectionClosed as e:
            if e.code != CLOSE_CODE_AUTH_FAILED:
                raise AssertionError(
                    'expected close code {}: got {}'.format(
                        CLOSE_CODE_AUTH_FAILED, e.code
                    )
                )
        else:
            raise AssertionError("expected connection to be closed")
