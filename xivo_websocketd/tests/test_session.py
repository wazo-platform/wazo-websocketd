# Copyright 2016-2017 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0+

import unittest

from unittest.mock import Mock

from hamcrest import assert_that, equal_to, is_

from xivo_websocketd.exception import NoTokenError
from xivo_websocketd.session import _extract_token_id
from xivo_websocketd.session import XMPPSessionCollection


class TestExtractTokenID(unittest.TestCase):

    def setUp(self):
        self.path = '/'
        self.websocket = Mock()
        self.websocket.raw_request_headers = []

    def test_token_id_in_path(self):
        self.path = '/?token=abcdef'

        self._assert_token_id_equal('abcdef')

    def test_token_id_in_header(self):
        self.websocket.raw_request_headers = [('X-Auth-Token', 'abcdef')]

        self._assert_token_id_equal('abcdef')

    def test_token_id_in_header_case_insensitive(self):
        self.websocket.raw_request_headers = [('x-auth-token', 'abcdef')]

        self._assert_token_id_equal('abcdef')

    def test_token_id_in_path_and_header_returns_path(self):
        self.path = '/?token=abc'
        self.websocket.raw_request_headers = [('X-Auth-Token', 'def')]

        self._assert_token_id_equal('abc')

    def test_no_token_id(self):
        self.assertRaises(NoTokenError, _extract_token_id, self.websocket, self.path)

    def _assert_token_id_equal(self, expected_token):
        token_id = _extract_token_id(self.websocket, self.path)

        assert_that(token_id, equal_to(expected_token))


class TestXMPPSessionCollection(unittest.TestCase):

    def setUp(self):
        self.sessions = XMPPSessionCollection()

    def test_find_by_username_when_no_session(self):
        session = self.sessions.find_by_username('123-456')
        assert_that(session, equal_to(None))

    def test_find_by_user_uuid(self):
        session = Mock(username='123-456')
        self.sessions.add(session)

        session = self.sessions.find_by_username('123-456')
        assert_that(session, is_(session))

    def test_find_by_user_uuid_when_multiple_sessions(self):
        session1 = Mock(username='123-456')
        session2 = Mock(username='789-012')
        self.sessions.add(session2)
        self.sessions.add(session1)

        session = self.sessions.find_by_username('123-456')
        assert_that(session, is_(session1))
