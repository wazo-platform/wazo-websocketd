# Copyright 2023-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import asyncio
import logging
import websockets

from logging.handlers import QueueHandler, QueueListener
from multiprocessing import Queue, get_context
from multiprocessing.context import SpawnProcess as Process, BaseContext
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


class ProcessWorker:
    def __init__(self, context: BaseContext, config: dict, *sync_vars):
        self._process: Process = context.Process(
            target=self._run_server, args=(config, *sync_vars)
        )

    def join(self):
        self._process.join()

    def start(self):
        self._process.start()

    def _run_server(
        self, config: dict, log_queue: Queue, master_tenant_proxy: Synchronized
    ):
        setproctitle('wazo-websocketd: worker process')
        self._setup_logging(config, log_queue)
        self._setup_master_tenant(master_tenant_proxy)

        # Manually manage loop instead of using `asyncio.run` because it is broken on uvloop 0.14.
        # Can be simplified after upgrading to any version above 0.14 (ex: Bookworm)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve(config))
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    @staticmethod
    async def _serve(config: dict):
        loop = asyncio.get_event_loop()
        server = WebsocketServer(config)
        loop.add_signal_handler(SIGINT, server.stop)
        loop.add_signal_handler(SIGTERM, server.stop)
        await server.serve()

    def _setup_master_tenant(self, proxy: Synchronized):
        MasterTenantProxy.proxy = proxy

    def _setup_logging(self, config: dict, log_queue: Queue):
        handler = QueueHandler(log_queue)
        process_logger = logging.getLogger()
        process_logger.handlers.clear()  # clear default handlers (file, stderr, etc)
        process_logger.addHandler(handler)  # send log messages to a queue
        process_logger.setLevel(
            logging.DEBUG if config.get('debug') else config['log_level']
        )
        silence_loggers(['aioamqp', 'urllib3'], logging.WARNING)


class ProcessPool:
    def __init__(self, config: dict):
        workers: int | str = config['process_workers']
        if workers == 'auto':
            workers = len(sched_getaffinity(0))

        if not isinstance(workers, int) or workers < 1:
            raise ValueError(
                'configuration key `process_workers` must be a positive integer or `auto`'
            )

        context = get_context('spawn')
        log_queue = context.JoinableQueue(10000)
        handlers = logging.getLogger().handlers
        self._log_listener = QueueListener(
            log_queue, *handlers, respect_handler_level=True
        )

        self._workers: set[ProcessWorker] = {
            ProcessWorker(context, config, log_queue, MasterTenantProxy.proxy)
            for _ in range(workers)
        }

    async def __aenter__(self):
        logger.info('starting %d worker process(es)', len(self._workers))
        self._log_listener.start()
        for worker in self._workers:
            worker.start()
        return self

    async def __aexit__(self, *args):
        for worker in self._workers:
            worker.join()
        logger.info('processing remaining log messages...')
        self._log_listener.stop()
