# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import Mock

import pytest

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


class TestSessionUserIdentity:
    def test_no_identity_before_authentication(self):
        session = _make_session()

        assert session.user_identity() == (None, None)

    def test_identity_after_authentication(self):
        session = _make_session()
        session._user_uuid = 'user-uuid-1234'
        session._tenant_uuid = 'tenant-uuid-5678'

        assert session.user_identity() == ('user-uuid-1234', 'tenant-uuid-5678')


class TestExtractTokenID:
    def setup_method(self):
        self.path = '/'
        self.websocket = Mock()
        self.websocket.request_headers.raw_items.return_value = []

    def test_token_id_in_path(self):
        self.path = '/?token=abcdef'

        assert _extract_token_id(self.websocket, self.path) == 'abcdef'

    def test_token_id_in_header(self):
        self.websocket.request_headers.raw_items.return_value = [
            ('X-Auth-Token', 'abcdef')
        ]

        assert _extract_token_id(self.websocket, self.path) == 'abcdef'

    def test_token_id_in_header_case_insensitive(self):
        self.websocket.request_headers.raw_items.return_value = [
            ('x-auth-token', 'abcdef')
        ]

        assert _extract_token_id(self.websocket, self.path) == 'abcdef'

    def test_token_id_in_path_and_header_returns_path(self):
        self.path = '/?token=abc'
        self.websocket.request_headers.raw_items.return_value = [
            ('X-Auth-Token', 'def')
        ]

        assert _extract_token_id(self.websocket, self.path) == 'abc'

    def test_no_token_id(self):
        with pytest.raises(NoTokenError):
            _extract_token_id(self.websocket, self.path)
