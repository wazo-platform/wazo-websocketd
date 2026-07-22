# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging
import socket
from asyncio import FIRST_COMPLETED, Future
from signal import SIGINT, SIGTERM

from .auth import Authenticator, MasterTenantProxy, ServiceTokenRenewer
from .bus import BusService
from .supervisor import Supervisor
from .token_cache import CachingAuthenticator

logger = logging.getLogger(__name__)


class Controller:
    def __init__(self, config: dict):
        self._config = config

    async def _initialize(self, tombstone: Future):
        async with BusService(self._config) as service:
            results = {
                asyncio.create_task(service.initialize_exchanges()),
                tombstone,
            }
            await asyncio.wait(results, return_when=FIRST_COMPLETED)

    def _create_listener(self) -> socket.socket:
        host = self._config['websocket']['listen']
        port = self._config['websocket']['port']
        family, socktype, proto, _, sockaddr = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )[0]
        listener = socket.socket(family, socktype, proto)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(sockaddr)
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
        return listener

    async def _run(self):
        tombstone: asyncio.Future = asyncio.Future()
        logger.info('wazo-websocketd starting...')

        loop = asyncio.get_event_loop()
        loop.add_signal_handler(SIGINT, tombstone.set_result, True)
        loop.add_signal_handler(SIGTERM, tombstone.set_result, True)

        await self._initialize(tombstone)

        if not tombstone.done():
            async with ServiceTokenRenewer(self._config) as token_renewer:
                token_renewer.subscribe(
                    MasterTenantProxy.set_master_tenant, details=True, oneshot=True
                )

                cache_config = self._config['token_cache']
                authenticator = CachingAuthenticator(
                    Authenticator(self._config),
                    positive_ttl=cache_config['positive_ttl'],
                    negative_ttl=cache_config['negative_ttl'],
                    max_size=cache_config['max_size'],
                    max_negative_entries=cache_config['max_negative_entries'],
                )
                listener = self._create_listener()
                try:
                    async with Supervisor(
                        self._config,
                        listener,
                        authenticator,
                        MasterTenantProxy.proxy,
                    ):
                        await tombstone
                finally:
                    listener.close()

        logger.info('wazo-websocketd stopped')

    def run(self):
        asyncio.run(self._run())
