# Copyright 2023-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import asyncio
import logging
import websockets

from itertools import repeat
from logging.handlers import QueueHandler, QueueListener
from multiprocessing import Queue, get_context
from multiprocessing.sharedctypes import Synchronized
from os import getpid, sched_getaffinity
from setproctitle import setproctitle
from signal import SIGINT, SIGTERM
from websockets.server import Serve
from xivo.xivo_logging import silence_loggers

from .auth import Authenticator, MasterTenantProxy
from .bus import BusService
from .protocol import SessionProtocolEncoder, SessionProtocolDecoder
from .session import SessionFactory


logger = logging.getLogger(__name__)


class WebsocketServer:
    def __init__(self, config: dict):
        self._config = config
        self._tombstone = asyncio.Future()

    def _create_server(self) -> tuple[BusService, Serve]:
        config = self._config
        authenticator: Authenticator = Authenticator(config)
        service: BusService = BusService(config)
        factory: SessionFactory = SessionFactory(
            config,
            authenticator,
            service,
            SessionProtocolEncoder(),
            SessionProtocolDecoder(),
        )

        host = config['websocket']['listen']
        port = config['websocket']['port']
        ssl = config['websocket']['ssl']

        server = websockets.serve(
            factory.ws_handler, host=host, port=port, ssl=ssl, reuse_port=True
        )

        return service, server

    async def serve(self):
        logger.info('starting websocket server on pid: %s', getpid())
        service, server = self._create_server()
        async with service, server:
            await self._tombstone
        logger.info('stopping websocket server on pid: %s', getpid())

    def stop(self):
        self._tombstone.set_result(True)


class ProcessPool:
    def __init__(self, config: dict):
        workers: int | str = config['process_workers']
        if workers == 'auto':
            workers = len(sched_getaffinity(0))

        if not isinstance(workers, int) or workers < 1:
            raise ValueError(
                'configuration key `process_workers` must be a positive integer or `auto`'
            )
        self._workers = workers
        self._config = config

        context = get_context('spawn')
        log_queue = context.Queue(1000)
        handlers = logging.getLogger().handlers
        self._log_listener = QueueListener(
            log_queue, *handlers, respect_handler_level=True
        )

        self._pool = context.Pool(
            workers, self._init_worker, (config, log_queue, MasterTenantProxy.proxy)
        )

    async def __aenter__(self):
        logger.info('starting %d worker process(es)', self._workers)
        self._log_listener.start()
        self._pool.map_async(self._run_worker, repeat(self._config, self._workers))
        return self

    async def __aexit__(self, *args):
        self._pool.close()
        self._pool.join()
        logger.info('processing remaining log messages...')
        self._log_listener.stop()

    @staticmethod
    def _init_worker(config: dict, log_queue: Queue, master_tenant_proxy: Synchronized):
        setproctitle('wazo-websocketd: worker')
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        MasterTenantProxy.proxy = master_tenant_proxy

        handler = QueueHandler(log_queue)
        process_logger = logging.getLogger()
        process_logger.addHandler(handler)
        process_logger.setLevel(
            logging.DEBUG if config.get('debug') else config['log_level']
        )
        silence_loggers(['aioamqp', 'urllib3'], logging.WARNING)

    @staticmethod
    def _run_worker(config: dict):
        loop = asyncio.get_event_loop()
        server = WebsocketServer(config)
        loop.add_signal_handler(SIGINT, server.stop)
        loop.add_signal_handler(SIGTERM, server.stop)

        try:
            loop.run_until_complete(server.serve())
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
