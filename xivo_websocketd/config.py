# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import argparse
import ssl

from xivo.chain_map import ChainMap
from xivo.config_helper import read_config_file_hierarchy
from xivo.xivo_logging import get_log_level_by_name


_DEFAULT_CONFIG = {
    'config_file': '/etc/xivo-websocketd/config.yml',
    'extra_config_files': '/etc/xivo-websocketd/conf.d/',
    'debug': False,
    'log_level': 'info',
    'log_file': '/var/log/xivo-websocketd.log',
    'foreground': False,
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
    },
    'exchanges': {
        'xivo': {
            'type': 'topic',
            'durable': True,
        },
    },
    'websocket': {
        'listen': '0.0.0.0',
        'port': 9502,
        'certificate': '/usr/share/xivo-certs/server.crt',
        'private_key': '/usr/share/xivo-certs/server.key',
        'ciphers': 'ALL:!aNULL:!eNULL:!LOW:!EXP:!RC4:!3DES:!SEED:+HIGH:+MEDIUM',
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
    parsed_args = parser.parse_args()

    result = {}
    if parsed_args.config_file:
        result['config_file'] = parsed_args.config_file
    if parsed_args.debug:
        result['debug'] = parsed_args.debug
    if parsed_args.foreground:
        result['foreground'] = parsed_args.foreground

    return result


def _get_reinterpreted_raw_values(config):
    result = {'websocket': {}}

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    ssl_context.load_cert_chain(config['websocket']['certificate'], config['websocket']['private_key'])
    ssl_context.set_ciphers(config['websocket']['ciphers'])
    result['websocket']['ssl'] = ssl_context

    result['log_level'] = get_log_level_by_name(config['log_level'])

    return result
