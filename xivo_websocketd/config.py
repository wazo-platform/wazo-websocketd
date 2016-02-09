# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import argparse
import ssl
import sys

from xivo.config_helper import read_config_file_hierarchy


def load_config():
    config = _new_default_config()
    _update_config_from_command_line(config)
    _update_config_from_file(config)
    _finalize_config(config)
    return config


def _new_default_config():
    return {
        'auth_config': {
            'host': 'localhost',
            'verify_certificate': '/usr/share/xivo-certs/server.crt',
        },
        'auth_check_strategy': 'static',
        'auth_check_static_interval': 60,
        'bus_host': 'localhost',
        'bus_port': 5672,
        'bus_username': 'guest',
        'bus_password': 'guest',
        'bus_exchanges': {
            'xivo': {
                'type': 'topic',
                'durable': True,
            },
        },
        'config_file': '/etc/xivo-websocketd/config.yml',
        'extra_config_files': '/etc/xivo-websocketd/conf.d',
        'ws_host': '0.0.0.0',
        'ws_port': 9502,
        'ws_certificate': '/usr/share/xivo-certs/server.crt',
        'ws_private_key': '/usr/share/xivo-certs/server.key',
        'ws_ciphers': 'ALL:!aNULL:!eNULL:!LOW:!EXP:!RC4:!3DES:!SEED:+HIGH:+MEDIUM',
        'ws_ping_interval': 60,
    }


def _update_config_from_command_line(config):
    parser = argparse.ArgumentParser()
    parser.add_argument('-c',
                        '--config-file',
                        action='store',
                        help="The path where is the config file.")

    parsed_args = parser.parse_args()
    if parsed_args.config_file:
        config['config_file'] = parsed_args.config_file


def _update_config_from_file(config):
    _update_config_from_file_config(config, read_config_file_hierarchy(config))


def _update_config_from_file_config(config, file_config):
    _update_config_from_file_root(config, file_config)
    _update_config_from_file_websocket_section(config, file_config.get('websocket'))
    _update_config_from_file_auth_section(config, file_config.get('auth'))
    _update_config_from_file_bus_section(config, file_config.get('bus'))
    _update_config_from_file_exchanges_section(config, file_config.get('exchanges'))


def _update_config_from_file_root(config, file_config):
    keys = [
        'auth_check_strategy',
        'auth_check_static_interval',
    ]
    for key in keys:
        if key in file_config:
            config[key] = file_config[key]


def _update_config_from_file_websocket_section(config, section):
    mapping = {
        'listen': 'ws_host',
        'port': 'ws_port',
        'certificate': 'ws_certificate',
        'private_key': 'ws_private_key',
        'ciphers': 'ws_ciphers',
        'ping_interval': 'ws_ping_interval',
    }
    _update_config_with_mapping(config, section, 'websocket', mapping)


def _update_config_from_file_auth_section(config, section):
    if not section:
        return

    config['auth_config'].update(section)


def _update_config_from_file_bus_section(config, section):
    mapping = {
        'host': 'bus_host',
        'port': 'bus_port',
        'username': 'bus_username',
        'password': 'bus_password',
    }
    _update_config_with_mapping(config, section, 'bus', mapping)


def _update_config_from_file_exchanges_section(config, section):
    if not section:
        return

    for exchange_name, value in section.items():
        exchange_config = dict(value)
        exchange_type = exchange_config.pop('type', None)
        if exchange_type is None:
            print('invalid option exchanges.{}: missing "type" option'.format(exchange_name), file=sys.stderr)
            continue
        exchange_durable = exchange_config.pop('durable', None)
        if exchange_durable is None:
            print('invalid option exchanges.{}: missing "durable" option'.format(exchange_name), file=sys.stderr)
            continue
        for key in exchange_config.keys():
            print('unknown option exchanges.{}.{}'.format(exchange_name, key), file=sys.stderr)
        config['bus_exchanges'][exchange_name] = {'type': exchange_type, 'durable': exchange_durable}


def _update_config_with_mapping(config, section, section_name, mapping):
    if not section:
        return

    for key, value in section.items():
        if key in mapping:
            config[mapping[key]] = value
        else:
            print('unknown option {}.{}'.format(section_name, key), file=sys.stderr)


def _finalize_config(config):
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    ssl_context.load_cert_chain(config['ws_certificate'], config['ws_private_key'])
    ssl_context.set_ciphers(config['ws_ciphers'])
    config['ws_ssl'] = ssl_context
