# Copyright 2016-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging
import unittest
from unittest import mock

from ..controller import Controller


class BusEventServiceMock:
    def __init__(self):
        self.closed = False

    async def close(self):
        await asyncio.sleep(0)
        self.closed = True


class TestControllerEvent(unittest.TestCase):
    def test_controller_start_stop(self):

        console = logging.StreamHandler()
        root = logging.getLogger('')
        root.setLevel(logging.DEBUG)
        root.addHandler(console)
        self.addCleanup(root.removeHandler, console)

        bus = BusEventServiceMock()
        ctrl = Controller(
            {"websocket": {"listen": "0.0.0.0", "port": 12345, "ssl": None}},
            bus,
            mock.Mock(),
        )
        ctrl.setup()
        asyncio.get_event_loop().call_later(1, ctrl._stop)
        ctrl.run()

        self.assertTrue(bus.closed)
