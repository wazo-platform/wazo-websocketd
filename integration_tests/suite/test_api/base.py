# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio

from xivo_test_helpers.asset_launching_test_case import AssetLaunchingTestCase

from .auth import AuthServer
from .constants import ASSET_ROOT
from .websocketd import WebSocketdClient 


class IntegrationTest(AssetLaunchingTestCase):

    assets_root = ASSET_ROOT
    service = 'websocketd'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        asyncio.set_event_loop(None)

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.websocketd_client = WebSocketdClient(self.loop)
        self.auth_server = AuthServer(self.loop)
