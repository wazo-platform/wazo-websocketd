# Copyright 2016-2021 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import functools

from xivo_test_helpers import asset_launching_test_case

from .auth import AuthServer
from .bus import BusClient
from .constants import ASSET_ROOT
from .websocketd import WebSocketdClient


class IntegrationTest(asset_launching_test_case.AssetLaunchingTestCase):

    assets_root = ASSET_ROOT
    service = 'websocketd'

    def setUp(self):
        self.valid_token_id = '123-456'
        self.loop = asyncio.get_event_loop()
        self.websocketd_client = self.new_websocketd_client()
        self.auth_server = self.new_auth_server()
        self.bus_client = self.new_bus_client()
        if self.auth_server:
            self.auth_server.put_token(
                self.valid_token_id, session_uuid='my-session', acl=['websocketd']
            )

    def tearDown(self):
        self.loop.run_until_complete(self.websocketd_client.close())
        self.loop.run_until_complete(self.bus_client.close())
        self.loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())

    def new_websocketd_client(self):
        websocketd_port = self.service_port(9502, 'websocketd')
        return WebSocketdClient(self.loop, websocketd_port)

    def new_auth_server(self):
        try:
            auth_port = self.service_port(9497, 'auth')
        except (
            asset_launching_test_case.NoSuchPort,
            asset_launching_test_case.NoSuchService,
        ):
            return None
        return AuthServer(self.loop, auth_port)

    def new_bus_client(self):
        bus_port = self.service_port(5672, 'rabbitmq')
        return BusClient(self.loop, bus_port)


def run_with_loop(f):
    # decorator to use on test methods of class deriving from IntegrationTest
    @functools.wraps(f)
    def wrapper(self, *args, **kwargs):
        self.loop.run_until_complete(
            asyncio.ensure_future(f(self, *args, **kwargs), loop=self.loop)
        )

    return wrapper
