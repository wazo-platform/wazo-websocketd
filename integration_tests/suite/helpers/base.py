# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import functools

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
    TOKEN_UUID,
    MASTER_USER_UUID,
    MASTER_TENANT_UUID,
    TENANT1_UUID,
    TENANT2_UUID,
)
from .websocketd import WebSocketdClient
from .wait_strategy import WaitStrategy, WaitUntilValidConnection


class ClientCreateException(Exception):
    def __init__(self, client_name):
        super().__init__(f'Could not create client {client_name}')


class WrongClient:
    def __init__(self, client_name):
        self.client_name = client_name

    def __getattr__(self, attr):
        raise ClientCreateException(self.client_name)


class IntegrationTest(AssetLaunchingTestCase):
    assets_root = ASSET_ROOT
    service = 'websocketd'

    # FIXME: Until a proper /status route is establish, wait a small amount
    # of time after creating service credentials to allow websocketd to find
    # who is the master tenant
    wait_strategy: WaitStrategy = WaitUntilValidConnection()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.auth_client = cls.make_auth()
        cls.configure_auth()
        cls.wait_strategy.wait(cls)

    def setUp(self):
        self.loop = asyncio.get_event_loop()
        self.bus_client = self.make_bus()
        self.websocketd_client = self.make_websocketd()

    def tearDown(self):
        self.loop.run_until_complete(self.websocketd_client.close())
        self.loop.run_until_complete(self.bus_client.close())
        self.loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())

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

    def make_websocketd(self):
        try:
            port = self.service_port(9502, 'websocketd')
        except (NoSuchService, NoSuchPort):
            return WrongClient('websocketd')
        return WebSocketdClient(port, loop=self.loop)


def run_with_loop(f):
    # decorator to use on test methods of class deriving from IntegrationTest
    @functools.wraps(f)
    def wrapper(self, *args, **kwargs):
        self.loop.run_until_complete(asyncio.ensure_future(f(self, *args, **kwargs)))

    return wrapper
