# Copyright 2016-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import ssl

from xivo.chain_map import ChainMap
from xivo.config_helper import read_config_file_hierarchy
from xivo.xivo_logging import get_log_level_by_name


_DEFAULT_CONFIG = {
    'config_file': '/etc/wazo-websocketd/config.yml',
    'extra_config_files': '/etc/wazo-websocketd/conf.d/',
    'debug': False,
    'foreground': False,
    'log_level': 'info',
    'log_file': '/var/log/wazo-websocketd.log',
    'user': 'wazo-websocketd',
    'pid_file': '/var/run/wazo-websocketd/wazo-websocketd.pid',
    'auth': {
        'host': 'localhost',
        'verify_certificate': '/usr/share/xivo-certs/server.crt',
    },
    'auth_check_strategy': 'static',
    'auth_check_static_interval': 60,
    'bus': {
        'host': 'localhost',
        'port': 5672,
        'username': 'guest',
        'password': 'guest',
        'exchange_name': 'xivo',
        'exchange_type': 'topic',
    },
    'websocket': {
        'listen': '0.0.0.0',
        'port': 9502,
        'certificate': '/usr/share/xivo-certs/server.crt',
        'private_key': '/usr/share/xivo-certs/server.key',
        'ping_interval': 60,
    },
}


def load_config():
    cli_config = _parse_cli_args()
    file_config = read_config_file_hierarchy(ChainMap(cli_config, _DEFAULT_CONFIG))
    reinterpreted_config = _get_reinterpreted_raw_values(ChainMap(cli_config, file_config, _DEFAULT_CONFIG))
    return ChainMap(reinterpreted_config, cli_config, file_config, _DEFAULT_CONFIG)


def _parse_cli_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c',
                        '--config-file',
                        action='store',
                        help="The path where is the config file")
    parser.add_argument('-d',
                        '--debug',
                        action='store_true',
                        help="Log debug messages. Overrides log_level.")
    parser.add_argument('-f',
                        '--foreground',
                        action='store_true',
                        help="Foreground, don't daemonize.")
    parser.add_argument('-u',
                        '--user',
                        action='store',
                        help="The owner of the process.")
    parsed_args = parser.parse_args()

    result = {}
    if parsed_args.config_file:
        result['config_file'] = parsed_args.config_file
    if parsed_args.debug:
        result['debug'] = parsed_args.debug
    if parsed_args.foreground:
        result['foreground'] = parsed_args.foreground
    if parsed_args.user:
        result['user'] = parsed_args.user

    return result


def _get_reinterpreted_raw_values(config):
    result = {'websocket': {}}

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    ssl_context.load_cert_chain(config['websocket']['certificate'], config['websocket']['private_key'])
    result['websocket']['ssl'] = ssl_context

    result['log_level'] = get_log_level_by_name(config['log_level'])

    return result
