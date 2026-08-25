# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import time
from uuid import uuid4

import websockets

from .helpers.base import (
    IntegrationTest,
    StaticAuthCheckAssetLaunchingTestCase,
    use_asset,
)
from .helpers.constants import (
    CLOSE_CODE_AUTH_EXPIRED,
    CLOSE_CODE_AUTH_FAILED,
    CLOSE_CODE_NO_TOKEN_ID,
    INVALID_TOKEN_ID,
    TOKEN_UUID,
    UNAUTHORIZED_TOKEN_ID,
)


@use_asset('base')
class TestAuthentication(IntegrationTest):
    async def test_no_token_closes_websocket(self):
        await self.websocketd_client.connect_and_wait_for_close(
            None, CLOSE_CODE_NO_TOKEN_ID
        )

    async def test_valid_auth_gives_result(self):
        with self.auth_client.token() as token:
            await self.websocketd_client.connect_and_wait_for_init(token)

    async def test_invalid_auth_closes_websocket(self):
        await self.websocketd_client.connect_and_wait_for_close(
            INVALID_TOKEN_ID, CLOSE_CODE_AUTH_FAILED
        )

    async def test_unauthorized_auth_closes_websocket(self):
        await self.websocketd_client.connect_and_wait_for_close(
            UNAUTHORIZED_TOKEN_ID, CLOSE_CODE_AUTH_FAILED
        )


@use_asset('base')
class TestNoAuth(IntegrationTest):
    async def test_no_auth_server_closes_websocket(self):
        async with self.asset_cls.service_stopped('auth'):
            await self.websocketd_client.connect_and_wait_for_close(TOKEN_UUID)


@use_asset('base')
class TestTokenExpirationCheckDynamic(IntegrationTest):
    _CLIENT_TIMEOUT = 20

    async def test_token_expire_use_dynamic_strategy(self):
        token_expiration = 1
        with self.auth_client.token(expiration=token_expiration) as token:
            await self.websocketd_client.connect_and_wait_for_init(token)
        self.websocketd_client.timeout = self._CLIENT_TIMEOUT
        await self.websocketd_client.wait_for_close(CLOSE_CODE_AUTH_EXPIRED)

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


@use_asset('static_auth_check')
class TestTokenExpirationCheckStatic(IntegrationTest):
    asset_cls = StaticAuthCheckAssetLaunchingTestCase

    _CLIENT_TIMEOUT = 5

    async def test_token_expire_use_static_strategy(self):
        session_uuid = uuid4()
        with self.auth_client.token(session_uuid=session_uuid) as token:
            await self.websocketd_client.connect_and_wait_for_init(token)
        await self.bus_client.publish_session_deleted(session_uuid)
        self.websocketd_client.timeout = self._CLIENT_TIMEOUT
        await self.websocketd_client.wait_for_close(CLOSE_CODE_AUTH_EXPIRED)


@use_asset('static_auth_check')
class TestTokenExpiration(IntegrationTest):
    asset_cls = StaticAuthCheckAssetLaunchingTestCase

    _TIMEOUT = 5

    async def test_token_expire_closes_websocket(self):
        session_uuid = uuid4()
        with self.auth_client.token(session_uuid=session_uuid) as token:
            await self.websocketd_client.connect_and_wait_for_init(token)
        await self.bus_client.publish_session_deleted(session_uuid)
        self.websocketd_client.timeout = self._TIMEOUT
        await self.websocketd_client.wait_for_close(CLOSE_CODE_AUTH_EXPIRED)

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

    async def test_token_renew_and_expire(self):
        session_uuid = uuid4()
        token = self.auth_client.make_token(session_uuid=session_uuid)
        await self.websocketd_client.connect_and_wait_for_init(token, version=2)
        await self.websocketd_client.op_start()
        new_token = self.auth_client.make_token(session_uuid=session_uuid)
        await self.websocketd_client.op_token(new_token)
        self.websocketd_client.timeout = self._TIMEOUT
        await self.websocketd_client.wait_for_nothing()
        self.auth_client.revoke_token(new_token)
        await self.bus_client.publish_session_deleted(session_uuid)
        self.websocketd_client.timeout = self._TIMEOUT
        await self.websocketd_client.wait_for_close(CLOSE_CODE_AUTH_EXPIRED)
        self.auth_client.revoke_token(token)

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
