# Copyright 2016-2022 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import logging

from xivo import xivo_logging
from xivo.user_rights import change_user

from wazo_websocketd.auth import Authenticator
from wazo_websocketd.bus import create_or_update_exchange, BusService
from wazo_websocketd.config import load_config
from wazo_websocketd.controller import Controller
from wazo_websocketd.protocol import SessionProtocolEncoder, SessionProtocolDecoder
from wazo_websocketd.session import SessionFactory


def main():
    config = load_config()

    xivo_logging.setup_logging(
        config['log_file'], debug=config['debug'], log_level=config['log_level']
    )
    xivo_logging.silence_loggers(['urllib3'], logging.WARNING)
    xivo_logging.silence_loggers(['aioamqp'], logging.WARNING)

    if config['user']:
        change_user(config['user'])

    create_or_update_exchange(config)
    authenticator = Authenticator(config)
    bus_service = BusService(config)
    protocol_encoder = SessionProtocolEncoder()
    protocol_decoder = SessionProtocolDecoder()
    session_factory = SessionFactory(
        config, authenticator, bus_service, protocol_encoder, protocol_decoder
    )
    controller = Controller(config, session_factory, bus_service)
    controller.setup()
    controller.run()
