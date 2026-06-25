# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import time

import websockets

from .helpers.base import IntegrationTest, run_with_loop
from .helpers.constants import (
    CLOSE_CODE_AUTH_EXPIRED,
    CLOSE_CODE_AUTH_FAILED,
    CLOSE_CODE_NO_TOKEN_ID,
    INVALID_TOKEN_ID,
    TENANT1_UUID,
    TOKEN_UUID,
    UNAUTHORIZED_TOKEN_ID,
    USER1_UUID,
)
from .helpers.wait_strategy import TimeWaitStrategy


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


class TestTokenExpirationCheckDynamic(IntegrationTest):
    asset = 'basic'

    _CLIENT_TIMEOUT = 20

    @run_with_loop
    async def test_token_expire_use_dynamic_strategy(self):
        token_expiration = 1
        with self.auth_client.token(expiration=token_expiration) as token:
            await self.websocketd_client.connect_and_wait_for_init(token)
        self.websocketd_client.timeout = self._CLIENT_TIMEOUT
        await self.websocketd_client.wait_for_close(CLOSE_CODE_AUTH_EXPIRED)

    @run_with_loop
    async def test_token_expire_new_token_is_received_use_dynamic_strategy(self):
        token_expiration = 2
        start = time.time()
        token = self.auth_client.make_token(expiration=token_expiration)
        await self.websocketd_client.connect_and_wait_for_init(token)
        await asyncio.sleep(1)
        self.websocketd_client.timeout = 10
        new_token = self.auth_client.make_token(expiration=token_expiration)
        await self.websocketd_client.op_token(new_token)
        await self.websocketd_client.wait_for_close(CLOSE_CODE_AUTH_EXPIRED)
        end = time.time()
        elapsed = end - start
        assert elapsed > token_expiration + 1


class TestTokenExpirationCheckStatic(IntegrationTest):
    asset = 'auth_check_static'

    _CLIENT_TIMEOUT = 15

    @run_with_loop
    async def test_token_expire_use_static_strategy(self):
        with self.auth_client.token() as token:
            await self.websocketd_client.connect_and_wait_for_init(token)
        self.websocketd_client.timeout = self._CLIENT_TIMEOUT
        await self.websocketd_client.wait_for_close(CLOSE_CODE_AUTH_EXPIRED)


class TestTokenExpiration(IntegrationTest):
    asset = 'auth_check_static'

    _TIMEOUT = 15

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


class TestTokenRevocation(IntegrationTest):
    asset = 'basic'

    @run_with_loop
    async def test_revoked_token_is_rejected_on_reconnect(self):
        token = self.auth_client.make_token(expiration=3600)
        await self.websocketd_client.connect_and_wait_for_init(token, version=2)
        await self.websocketd_client.op_start()

        self.auth_client.revoke_token(token)
        try:
            await self.websocketd_client.op_token(token)
        except websockets.ConnectionClosed as e:
            assert e.code == CLOSE_CODE_AUTH_FAILED, e.code
        else:
            raise AssertionError("expected connection to be closed")

        await asyncio.sleep(1)
        await self.websocketd_client.connect_and_wait_for_close(
            token, CLOSE_CODE_AUTH_FAILED
        )

    @run_with_loop
    async def test_session_deleted_event_closes_and_poisons_cache(self):
        token = self.auth_client.make_token(
            user_uuid=USER1_UUID, session_uuid='session-1', expiration=3600
        )
        await self.websocketd_client.connect_and_wait_for_init(token)

        await self.bus_client.connect()
        await self.bus_client.publish(
            {
                'name': 'auth_session_deleted',
                'uuid': 'session-1',
                'user_uuid': str(USER1_UUID),
                'tenant_uuid': str(TENANT1_UUID),
            },
            TENANT1_UUID,
            USER1_UUID,
        )
        self.websocketd_client.timeout = 10
        await self.websocketd_client.wait_for_close(CLOSE_CODE_AUTH_EXPIRED)

        await asyncio.sleep(1)
        await self.websocketd_client.connect_and_wait_for_close(
            token, CLOSE_CODE_AUTH_FAILED
        )
