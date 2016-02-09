# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import unittest
import sys

from unittest.mock import patch

from hamcrest import assert_that, equal_to

from xivo_websocketd.config import _update_config_from_file_config


class TestFileConfig(unittest.TestCase):

    def setUp(self):
        self.config = {}

    def test_empty_config(self):
        file_config = {}

        _update_config_from_file_config(self.config, file_config)

        assert_that(self.config, equal_to({}))

    def test_valid_root_options(self):
        file_config = {
            'auth_check_strategy': 'static',
            'auth_check_static_interval': 60,
        }
        expected_config = {
            'auth_check_strategy': 'static',
            'auth_check_static_interval': 60,
        }

        _update_config_from_file_config(self.config, file_config)

        assert_that(self.config, equal_to(expected_config))

    def test_valid_websocket_options(self):
        file_config = {
            'websocket': {
                'listen': '169.254.1.1',
                'port': 1234,
                'certificate': '/tmp/foo.crt',
                'private_key': '/tmp/foo.key',
                'ciphers': '!aNULL',
                'ping_interval': 75,
            }
        }
        expected_config = {
            'ws_host': '169.254.1.1',
            'ws_port': 1234,
            'ws_certificate': '/tmp/foo.crt',
            'ws_private_key': '/tmp/foo.key',
            'ws_ciphers': '!aNULL',
            'ws_ping_interval': 75,
        }

        _update_config_from_file_config(self.config, file_config)

        assert_that(self.config, equal_to(expected_config))

    @patch('builtins.print')
    def test_unknown_websocket_option_display_message(self, mocked_print):
        file_config = {'websocket': {'foo': 'bar'}}

        _update_config_from_file_config(self.config, file_config)

        assert_that(self.config, equal_to({}))
        mocked_print.assert_called_once_with('unknown option websocket.foo', file=sys.stderr)

    def test_valid_auth_options(self):
        # all options in the "auth" section are copied in the auth_config section,
        # to be used when instantiating a xivo_auth_client.Client
        self.config['auth_config'] = {}
        file_config = {
            'auth': {
                'foo': 'bar',
            }
        }
        expected_config = {
            'auth_config': {
                'foo': 'bar',
            }
        }

        _update_config_from_file_config(self.config, file_config)

        assert_that(self.config, equal_to(expected_config))

    def test_valid_bus_options(self):
        file_config = {
            'bus': {
                'host': '169.254.1.1',
                'port': 1234,
                'username': 'foo',
                'password': 'bar',
            }
        }
        expected_config = {
            'bus_host': '169.254.1.1',
            'bus_port': 1234,
            'bus_username': 'foo',
            'bus_password': 'bar',
        }

        _update_config_from_file_config(self.config, file_config)

        assert_that(self.config, equal_to(expected_config))

    @patch('builtins.print')
    def test_unknown_bus_option_display_message(self, mocked_print):
        file_config = {'bus': {'foo': 'bar'}}

        _update_config_from_file_config(self.config, file_config)

        assert_that(self.config, equal_to({}))
        mocked_print.assert_called_once_with('unknown option bus.foo', file=sys.stderr)

    def test_valid_exchange(self):
        self.config['bus_exchanges'] = {}
        file_config = {
            'exchanges': {
                'foo': {
                    'type': 'direct',
                    'durable': False,
                }
            }
        }
        expected_config = {
            'bus_exchanges': {
                'foo': {
                    'type': 'direct',
                    'durable': False,
                }
            }
        }

        _update_config_from_file_config(self.config, file_config)

        assert_that(self.config, equal_to(expected_config))

    @patch('builtins.print')
    def test_unknown_exchange_option_display_message(self, mocked_print):
        self.config['bus_exchanges'] = {}
        file_config = {
            'exchanges': {
                'foo': {
                    'type': 'direct',
                    'durable': False,
                    'bar': 'alice',
                }
            }
        }

        _update_config_from_file_config(self.config, file_config)

        mocked_print.assert_called_once_with('unknown option exchanges.foo.bar', file=sys.stderr)

    @patch('builtins.print')
    def test_missing_exchange_type_display_message(self, mocked_print):
        self.config['bus_exchanges'] = {}
        file_config = {
            'exchanges': {
                'foo': {
                    'durable': False,
                }
            }
        }
        expected_config = {'bus_exchanges': {}}

        _update_config_from_file_config(self.config, file_config)

        assert_that(self.config, equal_to(expected_config))
        mocked_print.assert_called_once_with('invalid option exchanges.foo: missing "type" option', file=sys.stderr)

    @patch('builtins.print')
    def test_missing_exchange_durable_display_message(self, mocked_print):
        self.config['bus_exchanges'] = {}
        file_config = {
            'exchanges': {
                'foo': {
                    'type': 'direct',
                }
            }
        }
        expected_config = {'bus_exchanges': {}}

        _update_config_from_file_config(self.config, file_config)

        assert_that(self.config, equal_to(expected_config))
        mocked_print.assert_called_once_with('invalid option exchanges.foo: missing "durable" option', file=sys.stderr)
