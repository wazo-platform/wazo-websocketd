# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging
import uvloop

from xivo import xivo_logging
from xivo.user_rights import change_user

from wazo_websocketd.config import load_config
from wazo_websocketd.controller import Controller


def main():
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    config = load_config()

    xivo_logging.setup_logging(
        config['log_file'], debug=config['debug'], log_level=config['log_level']
    )
    xivo_logging.silence_loggers(['urllib3'], logging.WARNING)
    xivo_logging.silence_loggers(['aioamqp'], logging.WARNING)

    if config['user']:
        change_user(config['user'])

    controller = Controller(config)
    controller.run()
