# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import websockets

from .helpers.wait_strategy import TimeWaitStrategy
from .helpers.base import IntegrationTest, run_with_loop
from .helpers.constants import (
    INVALID_TOKEN_ID,
    UNAUTHORIZED_TOKEN_ID,
    CLOSE_CODE_AUTH_FAILED,
    CLOSE_CODE_AUTH_EXPIRED,
    CLOSE_CODE_NO_TOKEN_ID,
    TOKEN_UUID,
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
        with self.auth_client.token() as token:
            await self.websocketd_client.connect_and_wait_for_init(token)

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


class TestNoAuth(IntegrationTest):
    asset = 'no_auth_server'
    wait_strategy = TimeWaitStrategy()

    @run_with_loop
    async def test_no_auth_server_closes_websocket(self):
        await self.websocketd_client.connect_and_wait_for_close(TOKEN_UUID)


class TestTokenExpiration(IntegrationTest):
    asset = 'token_expiration'

    _TIMEOUT = 15

    def setUp(self):
        super().setUp()
        self.token_id = 'dynamic-token'

    @run_with_loop
    async def test_token_expire_closes_websocket(self):
        with self.auth_client.token() as token:
            await self.websocketd_client.connect_and_wait_for_init(token)
        self.websocketd_client.timeout = self._TIMEOUT
        await self.websocketd_client.wait_for_close(CLOSE_CODE_AUTH_EXPIRED)

    @run_with_loop
    async def test_token_renew(self):
        token = self.auth_client.make_token()
        await self.websocketd_client.connect_and_wait_for_init(token, version=2)
        await self.websocketd_client.op_start()
        new_token = self.auth_client.make_token()
        await self.websocketd_client.op_token(new_token)
        self.auth_client.revoke_token(token)
        self.websocketd_client.timeout = self._TIMEOUT
        await self.websocketd_client.wait_for_nothing()
        self.auth_client.revoke_token(new_token)

    @run_with_loop
    async def test_token_renew_and_expire(self):
        token = self.auth_client.make_token()
        await self.websocketd_client.connect_and_wait_for_init(token, version=2)
        await self.websocketd_client.op_start()
        new_token = self.auth_client.make_token()
        await self.websocketd_client.op_token(new_token)
        self.websocketd_client.timeout = self._TIMEOUT
        await self.websocketd_client.wait_for_nothing()
        self.auth_client.revoke_token(new_token)
        self.websocketd_client.timeout = self._TIMEOUT
        await self.websocketd_client.wait_for_close(CLOSE_CODE_AUTH_EXPIRED)
        self.auth_client.revoke_token(token)

    @run_with_loop
    async def test_token_renew_invalid(self):
        with self.auth_client.token() as token:
            await self.websocketd_client.connect_and_wait_for_init(token, version=2)
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
