# Copyright 2016-2022 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging
import unittest

from unittest.mock import Mock

from ..controller import Controller


class BusServiceMock:
    def __enter__(self):
        pass

    def __exit__(self, *args):
        pass


class TestControllerEvent(unittest.TestCase):
    def test_controller_start_stop(self):
        config = {
            'websocket': {
                'listen': '0.0.0.0',
                'port': 1234,
                'ssl': None,
            },
            'auth': {
                'host': '0.0.0.0',
                'port': 2345,
            },
        }

        console = logging.StreamHandler()
        root = logging.getLogger('')
        root.setLevel(logging.DEBUG)
        root.addHandler(console)
        self.addCleanup(root.removeHandler, console)

        ctrl = Controller(config, Mock(), BusServiceMock())
        ctrl.setup()
        asyncio.get_event_loop().call_later(1, ctrl._stop)
        ctrl.run()
