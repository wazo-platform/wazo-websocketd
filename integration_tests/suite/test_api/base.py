# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio

from xivo_test_helpers.asset_launching_test_case import AssetLaunchingTestCase

from .auth import AuthServer
from .bus import BusClient
from .constants import ASSET_ROOT
from .websocketd import WebSocketdClient


class IntegrationTest(AssetLaunchingTestCase):

    assets_root = ASSET_ROOT
    service = 'websocketd'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # There is bugs in asynqp 0.4 that prevent from setting the event_loop
        # to None
        #asyncio.set_event_loop(None)

    def setUp(self):
        #self.loop = asyncio.new_event_loop()
        self.loop = asyncio.get_event_loop()
        self.websocketd_client = WebSocketdClient(self.loop)
        self.auth_server = AuthServer(self.loop)
        self.bus_client = BusClient(self.loop)

    def tearDown(self):
        self.loop.run_until_complete(self.websocketd_client.close())
        self.loop.run_until_complete(self.bus_client.close())
        #self.loop.close()
