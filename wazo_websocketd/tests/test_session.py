# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest
from unittest.mock import Mock

from hamcrest import assert_that, equal_to

from ..exception import NoTokenError
from ..session import Session, _extract_token_id

_CONFIG = {'websocket': {'ping_interval': 30}}


def _make_session():
    return Session(
        _CONFIG,
        Mock(),
        Mock(),
        Mock(),
        Mock(),
        Mock(),
        '/',
    )


class TestSessionUserIdentity(unittest.TestCase):
    def test_no_identity_before_authentication(self):
        session = _make_session()

        user_uuid, tenant_uuid = session.user_identity()

        assert_that(user_uuid, equal_to(None))
        assert_that(tenant_uuid, equal_to(None))

    def test_identity_after_authentication(self):
        session = _make_session()
        session._user_uuid = 'user-uuid-1234'
        session._tenant_uuid = 'tenant-uuid-5678'

        user_uuid, tenant_uuid = session.user_identity()

        assert_that(user_uuid, equal_to('user-uuid-1234'))
        assert_that(tenant_uuid, equal_to('tenant-uuid-5678'))


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
