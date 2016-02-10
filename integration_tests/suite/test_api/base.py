# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio

import asynqp

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
        self.loop = asyncio.get_event_loop()
        self.loop.set_exception_handler(self.__exception_handler)
        self.websocketd_client = WebSocketdClient(self.loop)
        self.auth_server = AuthServer(self.loop)
        self.bus_client = BusClient(self.loop)

    def tearDown(self):
        self.loop.run_until_complete(self.websocketd_client.close())
        self.loop.run_until_complete(self.bus_client.close())
        self.loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())

    def __exception_handler(self, loop, context):
        exception = context.get('exception')
        if isinstance(exception, asynqp.exceptions.ConnectionClosedError):
            print('debug: got asynqp ConnectionClosedError (happens on normal close)')
        else:
            loop.default_exception_handler(context)
