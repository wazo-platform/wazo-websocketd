# Copyright 2016-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging

from xivo import xivo_logging
from xivo.daemonize import pidfile_context
from xivo.user_rights import change_user

from wazo_websocketd.auth import Authenticator
from wazo_websocketd.bus import new_bus_event_service
from wazo_websocketd.config import load_config
from wazo_websocketd.controller import Controller
from wazo_websocketd.protocol import SessionProtocolEncoder, SessionProtocolDecoder
from wazo_websocketd.session import SessionFactory


def main():
    config = load_config()

    xivo_logging.setup_logging(
        config['log_file'], config['foreground'], config['debug'], config['log_level']
    )
    xivo_logging.silence_loggers(['urllib3'], logging.WARNING)

    if config['user']:
        change_user(config['user'])

    loop = asyncio.get_event_loop()
    authenticator = Authenticator(config, loop)
    bus_event_service = new_bus_event_service(config, loop)
    protocol_encoder = SessionProtocolEncoder()
    protocol_decoder = SessionProtocolDecoder()
    session_factory = SessionFactory(
        config,
        loop,
        authenticator,
        bus_event_service,
        protocol_encoder,
        protocol_decoder,
    )
    controller = Controller(config, loop, bus_event_service, session_factory)

    with pidfile_context(config['pid_file'], config['foreground']):
        controller.setup()
        controller.run()
