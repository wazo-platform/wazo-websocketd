# Copyright 2016-2022 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import signal
import logging
import websockets

from .auth import ServiceTokenRenewer, set_master_tenant

logger = logging.getLogger(__name__)


class _WebsocketServerManager:
    def __init__(self, factory, host, port, *, ssl=None, loop=None):
        self._loop = loop or asyncio.get_event_loop()
        self._start = websockets.serve(factory, host, port, ssl=ssl)
        self._server = None

    def __enter__(self):
        self._server = self._loop.run_until_complete(self._start)
        logger.info('websocket now serving connections')

    def __exit__(self, *args):
        self._server.close()
        self._loop.run_until_complete(
            asyncio.ensure_future(
                self._server.wait_closed(),
            )
        )
        logger.info('websocket server terminated')


class Controller:
    def __init__(self, config, session_factory, bus_service):
        self._ws_host = config['websocket']['listen']
        self._ws_port = config['websocket']['port']
        self._ws_ssl = config['websocket']['ssl']
        self._session_factory = session_factory
        self._bus_service = bus_service
        self._ws_server = _WebsocketServerManager(
            self._session_factory.ws_handler,
            self._ws_host,
            self._ws_port,
            ssl=self._ws_ssl,
        )
        self._token_renewer = ServiceTokenRenewer(config)

    def setup(self):
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT, self._stop)
        loop.add_signal_handler(signal.SIGTERM, self._stop)
        self._token_renewer.subscribe(set_master_tenant, details=True, oneshot=True)

    def run(self):
        logger.info('wazo-websocketd starting...')
        loop = asyncio.get_event_loop()
        try:
            with self._token_renewer:
                with self._bus_service:
                    with self._ws_server:
                        loop.run_forever()
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            logger.info('wazo-websocketd stopped')

    def _stop(self):
        logger.info('wazo-websocketd stopping...')
        asyncio.get_event_loop().stop()
