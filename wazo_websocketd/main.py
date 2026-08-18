# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging
import sys

from wazo_websocketd.config import load_config
from wazo_websocketd.helpers.process import bootstrap, drop_privileges
from wazo_websocketd.supervisor import Supervisor

logger = logging.getLogger(__name__)


def main():
    config = load_config()

    bootstrap(config, 'supervisor')
    drop_privileges(config)

    supervisor = Supervisor(config)
    asyncio.run(supervisor.run())
    if supervisor.failed:
        sys.exit(1)
