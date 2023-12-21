# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest
from unittest.mock import Mock

from hamcrest import assert_that, equal_to

from ..exception import NoTokenError
from ..session import _extract_token_id


class TestExtractTokenID(unittest.TestCase):
    def setUp(self):
        self.path = '/'
        self.websocket = Mock()

    def test_token_id_in_path(self):
        self.path = '/?token=abcdef'

        self._assert_token_id_equal('abcdef')

    def test_token_id_in_header(self):
        self.websocket.request_headers.raw_items.return_value = [
            ('X-Auth-Token', 'abcdef')
        ]

        self._assert_token_id_equal('abcdef')

    def test_token_id_in_header_case_insensitive(self):
        self.websocket.request_headers.raw_items.return_value = [
            ('x-auth-token', 'abcdef')
        ]

        self._assert_token_id_equal('abcdef')

    def test_token_id_in_path_and_header_returns_path(self):
        self.path = '/?token=abc'
        self.websocket.request_headers.raw_items.return_value = [
            ('X-Auth-Token', 'def')
        ]

        self._assert_token_id_equal('abc')

    def test_no_token_id(self):
        self.websocket.request_headers.raw_items.return_value = []
        self.assertRaises(NoTokenError, _extract_token_id, self.websocket, self.path)

    def _assert_token_id_equal(self, expected_token):
        token_id = _extract_token_id(self.websocket, self.path)

        assert_that(token_id, equal_to(expected_token))
