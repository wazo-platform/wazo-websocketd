# Copyright 2016-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import signal
import logging

import asynqp
import websockets

logger = logging.getLogger(__name__)


class Controller(object):
    def __init__(self, config, bus_event_service, session_factory):
        self._ws_host = config['websocket']['listen']
        self._ws_port = config['websocket']['port']
        self._ws_ssl = config['websocket'].get('ssl')
        self._bus_event_service = bus_event_service
        self._session_factory = session_factory

    def setup(self):
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT, self._stop)
        loop.add_signal_handler(signal.SIGTERM, self._stop)
        loop.set_exception_handler(self._exception_handler)
        start_ws_server = websockets.serve(
            self._session_factory.ws_handler,
            self._ws_host,
            self._ws_port,
            ssl=self._ws_ssl,
        )
        self._ws_server = loop.run_until_complete(start_ws_server)

    def run(self):
        logger.info('wazo-websocketd starting...')
        loop = asyncio.get_event_loop()
        try:
            loop.run_forever()
        finally:
            loop.run_until_complete(
                asyncio.gather(
                    asyncio.ensure_future(self._ws_server.wait_closed(), loop=loop),
                    asyncio.ensure_future(self._bus_event_service.close(), loop=loop),
                )
            )
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            logger.info('wazo-websocketd stopped')

    def _exception_handler(self, loop, context):
        exception = context.get('exception')
        if isinstance(exception, asynqp.exceptions.ConnectionLostError):
            logger.warning('bus connection has been lost')
            self._bus_event_service.on_connection_lost()
        else:
            loop.default_exception_handler(context)

    def _stop(self):
        logger.info('wazo-websocketd stopping...')
        self._ws_server.close()
        asyncio.get_event_loop().stop()
