# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging
import sys

from wazo_websocketd.config import load_config
from wazo_websocketd.helpers.process import bootstrap, drop_privileges
from wazo_websocketd.supervisor import Supervisor, prepare_runtime_dir

logger = logging.getLogger(__name__)


def main():
    config = load_config()

    bootstrap(config, 'supervisor')
    prepare_runtime_dir(config['user'])  # chowns, so still as root
    drop_privileges(config)

    supervisor = Supervisor(config)
    asyncio.run(supervisor.run())
    if supervisor.failed:
        sys.exit(1)
