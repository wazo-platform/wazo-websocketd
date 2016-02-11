# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import signal
import logging

import asynqp
import websockets

logger = logging.getLogger(__name__)


class Controller(object):

    def __init__(self, config, loop, session_factory):
        self._ws_host = config['websocket']['listen']
        self._ws_port = config['websocket']['port']
        self._ws_ssl = config['websocket']['ssl']
        self._loop = loop
        self._session_factory = session_factory

    def setup(self):
        self._loop.add_signal_handler(signal.SIGINT, self._stop)
        self._loop.add_signal_handler(signal.SIGTERM, self._stop)
        self._loop.set_exception_handler(self._exception_handler)
        start_ws_server = websockets.serve(self._session_factory.ws_handler, self._ws_host, self._ws_port, ssl=self._ws_ssl, loop=self._loop)
        self._ws_server = self._loop.run_until_complete(start_ws_server)

    def run(self):
        logger.info('xivo-websocketd starting...')
        try:
            self._loop.run_forever()
        finally:
            logger.info('xivo-websocketd stopping...')
            self._loop.close()

    def _exception_handler(self, loop, context):
        exception = context.get('exception')
        if isinstance(exception, asynqp.exceptions.ConnectionLostError):
            self._session_factory.on_bus_connection_lost()
        elif isinstance(exception, asynqp.exceptions.ConnectionClosedError):
            pass
        else:
            loop.default_exception_handler(context)

    def _stop(self):
        self._ws_server.close()
        self._loop.create_task(self._wait_closed())

    @asyncio.coroutine
    def _wait_closed(self):
        yield from self._ws_server.wait_closed()
        self._loop.stop()
