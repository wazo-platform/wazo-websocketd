# Copyright 2016-2022 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import signal
import logging
import websockets

from .auth import TokenRenewer, set_master_tenant

logger = logging.getLogger(__name__)


class Controller(object):
    def __init__(self, config, session_factory):
        self._ws_host = config['websocket']['listen']
        self._ws_port = config['websocket']['port']
        self._ws_ssl = config['websocket']['ssl']
        self._session_factory = session_factory
        self._token_renewer = TokenRenewer(config)

    def setup(self):
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGINT, self._stop)
        loop.add_signal_handler(signal.SIGTERM, self._stop)
        start_ws_server = websockets.serve(
            self._session_factory.ws_handler,
            self._ws_host,
            self._ws_port,
            ssl=self._ws_ssl,
        )
        self._ws_server = loop.run_until_complete(start_ws_server)
        self._token_renewer.subscribe(set_master_tenant, details=True)

    def run(self):
        logger.info('wazo-websocketd starting...')
        loop = asyncio.get_event_loop()
        try:
            with self._token_renewer:
                loop.run_forever()
        finally:
            loop.run_until_complete(
                asyncio.gather(
                    asyncio.ensure_future(self._ws_server.wait_closed()),
                )
            )
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            logger.info('wazo-websocketd stopped')

    def _stop(self):
        logger.info('wazo-websocketd stopping...')
        self._ws_server.close()
        asyncio.get_event_loop().stop()
