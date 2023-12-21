# Copyright 2023-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import asyncio
import logging
from ctypes import c_wchar
from multiprocessing import get_context
from os import chdir, getpid, sched_getaffinity
from signal import SIGINT, SIGTERM
from tempfile import TemporaryDirectory

import websockets
from setproctitle import setproctitle
from websockets.server import Serve
from xivo.xivo_logging import setup_logging, silence_loggers

from .auth import Authenticator, MasterTenantProxy
from .bus import BusService
from .protocol import SessionProtocolDecoder, SessionProtocolEncoder
from .session import SessionFactory

logger = logging.getLogger(__name__)


class WebsocketServer:
    def __init__(self, config: dict):
        self._config = config
        self._tombstone: asyncio.Future = asyncio.Future()

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
        self._dir = TemporaryDirectory(prefix="wazo-websocketd-")

        context = get_context('spawn')
        chdir(self._dir.name)
        self._pool = context.Pool(
            workers, self._init_worker, (config, MasterTenantProxy.proxy)
        )

    async def __aenter__(self):
        logger.info('starting %d worker process(es)', self._workers)
        for _ in range(self._workers):
            self._pool.apply_async(self._run, (self._config,))
        return self

    async def __aexit__(self, *args):
        self._pool.close()
        self._pool.join()
        self._dir.cleanup()

    @staticmethod
    def _init_worker(config: dict, master_tenant_proxy: c_wchar):
        setproctitle('wazo-websocketd: worker')
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        MasterTenantProxy.proxy = master_tenant_proxy

        setup_logging(
            config['log_file'], debug=config['debug'], log_level=config['log_level']
        )
        silence_loggers(['aioamqp', 'urllib3', 'stevedore.extension'], logging.WARNING)

    @staticmethod
    def _run(config: dict):
        server = WebsocketServer(config)
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(SIGINT, server.stop)
        loop.add_signal_handler(SIGTERM, server.stop)

        # Manually manage loop instead of using `asyncio.run` because it is broken on uvloop 0.14.
        # Can be simplified after upgrading to any version above 0.14 (ex: Bookworm)
        try:
            loop.run_until_complete(server.serve())
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
