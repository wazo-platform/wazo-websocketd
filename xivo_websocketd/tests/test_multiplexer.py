# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import unittest

from xivo_websocketd.multiplexer import Multiplexer


class TestMultiplexer(unittest.TestCase):

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.multiplexer = Multiplexer(self.loop)

    def test_call_soon_with_callback(self):
        def callback():
            self.multiplexer.stop()

        self.multiplexer.call_soon(callback)

        self.loop.run_until_complete(self._run_multiplexer())

    def test_call_soon_with_args(self):
        def callback(s):
            self.multiplexer.stop()

        self.multiplexer.call_soon(callback, 'foo')

        self.loop.run_until_complete(self._run_multiplexer())

    def test_call_soon_with_coroutinefunction(self):
        @asyncio.coroutine
        def coroutine():
            yield from asyncio.sleep(0.01, loop=self.loop)
            self.multiplexer.stop()

        self.multiplexer.call_soon(coroutine)

        self.loop.run_until_complete(self._run_multiplexer())

    def test_call_later_with_callback(self):
        def callback():
            self.multiplexer.stop()

        self.multiplexer.call_later(0.01, callback)

        self.loop.run_until_complete(self._run_multiplexer())

    def test_call_later_with_coroutinefunction(self):
        @asyncio.coroutine
        def coroutine():
            yield from asyncio.sleep(0.01, loop=self.loop)
            self.multiplexer.stop()

        self.multiplexer.call_later(0.01, coroutine)

        self.loop.run_until_complete(self._run_multiplexer())

    @asyncio.coroutine
    def _run_multiplexer(self):
        try:
            yield from self.multiplexer.run()
        finally:
            yield from self.multiplexer.close()
