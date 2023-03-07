# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import uvloop
import asyncio

from xivo import xivo_logging
from xivo.user_rights import change_user

from wazo_websocketd.auth import Authenticator
from wazo_websocketd.bus import BusService
from wazo_websocketd.config import load_config
from wazo_websocketd.controller import Controller
from wazo_websocketd.protocol import SessionProtocolEncoder, SessionProtocolDecoder
from wazo_websocketd.session import SessionFactory


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

    authenticator = Authenticator(config)
    bus_service = BusService(config['bus'])
    protocol_encoder = SessionProtocolEncoder()
    protocol_decoder = SessionProtocolDecoder()
    session_factory = SessionFactory(
        config, authenticator, bus_service, protocol_encoder, protocol_decoder
    )
    controller = Controller(config, session_factory, bus_service)
    controller.setup()
    controller.run()
