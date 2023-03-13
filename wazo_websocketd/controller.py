# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging

from signal import SIGINT, SIGTERM

from .auth import ServiceTokenRenewer, set_master_tenant
from .bus import BusService
from .process import ProcessPool

logger = logging.getLogger(__name__)


class Controller:
    def __init__(self, config):
        self._config = config

    async def _run(self):
        tombstone = asyncio.Future()
        logger.info('wazo-websocketd starting...')

        loop = asyncio.get_event_loop()
        loop.add_signal_handler(SIGINT, tombstone.set_result, True)
        loop.add_signal_handler(SIGTERM, tombstone.set_result, True)

        async with BusService(self._config) as service:
            await service.initialize_exchanges()

        async with ServiceTokenRenewer(self._config) as token_renewer:
            token_renewer.subscribe(set_master_tenant, details=True, oneshot=True)

            async with ProcessPool(self._config):
                await tombstone  # wait for SIGTERM or SIGINT

        logger.info('wazo-websocketd stopped')

    def run(self):
        asyncio.run(self._run())
