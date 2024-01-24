# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging
from asyncio import FIRST_COMPLETED, Future
from signal import SIGINT, SIGTERM

from .auth import MasterTenantProxy, ServiceTokenRenewer
from .bus import BusService
from .process import ProcessPool

logger = logging.getLogger(__name__)


class Controller:
    def __init__(self, config: dict):
        self._config = config

    async def _initialize(self, tombstone: Future):
        async with BusService(self._config) as service:
            results = {service.initialize_exchanges(), tombstone}
            await asyncio.wait(results, return_when=FIRST_COMPLETED)

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

                async with ProcessPool(self._config):
                    await tombstone  # wait for SIGTERM or SIGINT

        logger.info('wazo-websocketd stopped')

    def run(self):
        asyncio.run(self._run())
