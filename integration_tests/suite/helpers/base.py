# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import unittest
from contextlib import asynccontextmanager

import aiohttp
import pytest
from wazo_test_helpers.asset_launching_test_case import (
    AssetLaunchingTestCase,
    NoSuchPort,
    NoSuchService,
)
from wazo_test_helpers.auth import MockCredentials
from wazo_test_helpers.filesystem import FileSystemClient

from .auth import AuthClient
from .bus import BusClient
from .constants import (
    ASSET_ROOT,
    MASTER_TENANT_UUID,
    MASTER_USER_UUID,
    START_TIMEOUT,
    TENANT1_UUID,
    TENANT2_UUID,
    TOKEN_UUID,
)
from .wait_strategy import AsyncWaitStrategy, BrokerReady, WebsocketdReady
from .websocketd import WebSocketdClient

use_asset = pytest.mark.usefixtures


class ClientCreateException(Exception):
    def __init__(self, client_name):
        super().__init__(f'Could not create client {client_name}')


class WrongClient:
    def __init__(self, client_name):
        self.client_name = client_name

    def __getattr__(self, attr):
        raise ClientCreateException(self.client_name)


class _BaseAssetLaunchingTestCase(AssetLaunchingTestCase):
    assets_root = ASSET_ROOT
    service = 'websocketd'
    asset = 'base'

    # FIXME: Until a proper /status route is establish, wait a small amount
    # of time after creating service credentials to allow websocketd to find
    # who is the master tenant
    wait_strategy = AsyncWaitStrategy(BrokerReady(), WebsocketdReady())

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.auth_client = cls.make_auth()
        cls.configure_auth()
        cls.wait_strategy.wait(cls)

    @classmethod
    def configure_auth(cls):
        if isinstance(cls.auth_client, WrongClient):
            return

        token = cls.auth_client.make_token(
            token_uuid=TOKEN_UUID,
            user_uuid=MASTER_USER_UUID,
            tenant_uuid=MASTER_TENANT_UUID,
        )
        credentials = MockCredentials('websocketd-service', 'websocketd-password')
        cls.auth_client.set_valid_credentials(credentials, token)
        cls.auth_client.set_tenants(
            {
                'uuid': str(MASTER_TENANT_UUID),
                'name': 'master tenant',
                'parent_uuid': str(MASTER_TENANT_UUID),
            },
            {
                'uuid': str(TENANT1_UUID),
                'name': 'some tenant',
                'parent_uuid': str(MASTER_TENANT_UUID),
            },
            {
                'uuid': str(TENANT2_UUID),
                'name': 'some other tenant',
                'parent_uuid': str(MASTER_TENANT_UUID),
            },
        )

    @classmethod
    @asynccontextmanager
    async def service_stopped(cls, service):
        cls.stop_service(service)
        try:
            yield
        finally:
            cls.start_service(service)
            if service == 'auth':
                # the restarted mock lost its in-memory state and may map a
                # new host port: remake the client and reconfigure it
                cls.auth_client = cls.make_auth()
                for _ in range(START_TIMEOUT):
                    if cls.auth_client.is_up():
                        break
                    await asyncio.sleep(1)
                cls.configure_auth()
            await cls.wait_strategy.await_ready(cls)

    @classmethod
    def make_filesystem(cls):
        return FileSystemClient(execute=cls.docker_exec)

    @classmethod
    def make_auth(cls):
        try:
            port = cls.service_port(9497, 'auth')
        except (NoSuchService, NoSuchPort):
            return WrongClient('auth')
        return AuthClient(port)

    @classmethod
    def make_bus(cls):
        try:
            port = cls.service_port(5672, 'rabbitmq')
        except (NoSuchService, NoSuchPort):
            return WrongClient('rabbitmq')
        return BusClient(port)

    @classmethod
    async def broker_status(cls, timeout=5.0):
        port = cls.service_port(9504, 'broker')
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(f'http://127.0.0.1:{port}/status') as response:
                return response.status, await response.json()

    @classmethod
    def make_websocketd(cls, loop):
        try:
            port = cls.service_port(9502, 'websocketd')
        except (NoSuchService, NoSuchPort):
            return WrongClient('websocketd')
        return WebSocketdClient(port, loop=loop)


class BaseAssetLaunchingTestCase(_BaseAssetLaunchingTestCase):
    pass


class StaticAuthCheckAssetLaunchingTestCase(_BaseAssetLaunchingTestCase):
    asset = 'static_auth_check'


class IntegrationTest(unittest.IsolatedAsyncioTestCase):
    asset_cls: type[_BaseAssetLaunchingTestCase] = BaseAssetLaunchingTestCase

    async def asyncSetUp(self):
        self.auth_client = self.asset_cls.auth_client
        self.bus_client = self.asset_cls.make_bus()
        self.websocketd_client = self.asset_cls.make_websocketd(
            asyncio.get_running_loop()
        )

    async def asyncTearDown(self):
        await self.websocketd_client.close()
        await self.bus_client.close()
