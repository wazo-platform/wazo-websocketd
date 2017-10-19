# Copyright 2016-2017 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import functools

import asynqp

from xivo_test_helpers import asset_launching_test_case

from .auth import AuthServer
from .bus import BusClient
from .constants import ASSET_ROOT
from .websocketd import WebSocketdClient
from .mongooseim import MongooseIMClient


class IntegrationTest(asset_launching_test_case.AssetLaunchingTestCase):

    assets_root = ASSET_ROOT
    service = 'websocketd'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # There is bugs in asynqp 0.4 that prevent from setting the event_loop
        # to None
        # asyncio.set_event_loop(None)

    def setUp(self):
        self.valid_token_id = '123-456'
        self.loop = asyncio.get_event_loop()
        self.loop.set_exception_handler(self.__exception_handler)
        self.websocketd_client = self.new_websocketd_client()
        self.auth_server = self.new_auth_server()
        self.bus_client = self.new_bus_client()
        self.mongooseim_client = self.new_mongooseim_client()
        if self.auth_server:
            self.loop.run_until_complete(self.auth_server.put_token(self.valid_token_id))

    def tearDown(self):
        self.loop.run_until_complete(self.websocketd_client.close())
        self.loop.run_until_complete(self.bus_client.close())
        self.loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())

    def __exception_handler(self, loop, context):
        exception = context.get('exception')
        if isinstance(exception, asynqp.exceptions.ConnectionClosedError):
            # this happens even on normal close
            print('debug: got asynqp ConnectionClosedError')
        else:
            loop.default_exception_handler(context)

    def new_websocketd_client(self):
        websocketd_port = self.service_port(9502, 'websocketd')
        return WebSocketdClient(self.loop, websocketd_port)

    def new_auth_server(self):
        try:
            auth_port = self.service_port(9497, 'auth')
        except (asset_launching_test_case.NoSuchPort, asset_launching_test_case.NoSuchService):
            return None
        return AuthServer(self.loop, auth_port)

    def new_bus_client(self):
        bus_port = self.service_port(5672, 'rabbitmq')
        return BusClient(self.loop, bus_port)

    def new_mongooseim_client(self):
        try:
            mongooseim_port = self.service_port(8088, 'mongooseim')
        except (asset_launching_test_case.NoSuchPort, asset_launching_test_case.NoSuchService):
            mongooseim_port = None
        return MongooseIMClient(mongooseim_port)


def run_with_loop(f):
    # decorator to use on test methods of class deriving from IntegrationTest
    @functools.wraps(f)
    def wrapper(self, *args, **kwargs):
        coro = asyncio.coroutine(f)
        self.loop.run_until_complete(coro(self, *args, **kwargs))
    return wrapper
