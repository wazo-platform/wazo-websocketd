# Copyright 2016-2017 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import logging

from xivo import xivo_logging
from xivo.daemonize import pidfile_context
from xivo.user_rights import change_user

from xivo_websocketd.auth import new_authenticator
from xivo_websocketd.bus import new_bus_event_service
from xivo_websocketd.config import load_config
from xivo_websocketd.controller import Controller
from xivo_websocketd.protocol import SessionProtocolEncoder,\
    SessionProtocolDecoder
from xivo_websocketd.session import SessionFactory, SessionCollection


def main():
    config = load_config()

    xivo_logging.setup_logging(config['log_file'], config['foreground'], config['debug'], config['log_level'])
    xivo_logging.silence_loggers(['urllib3'], logging.WARNING)

    if config['user']:
        change_user(config['user'])

    loop = asyncio.get_event_loop()
    authenticator = new_authenticator(config, loop)
    bus_event_service = new_bus_event_service(config, loop)
    protocol_encoder = SessionProtocolEncoder()
    protocol_decoder = SessionProtocolDecoder()
    session_collection = SessionCollection()
    session_factory = SessionFactory(config, loop, authenticator, bus_event_service,
                                     protocol_encoder, protocol_decoder, session_collection)
    controller = Controller(config, loop, bus_event_service, session_factory)

    with pidfile_context(config['pid_file'],  config['foreground']):
        controller.setup()
        controller.run()
