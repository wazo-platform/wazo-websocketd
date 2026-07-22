# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest
from unittest.mock import AsyncMock, Mock, patch

import pytest
from hamcrest import assert_that, equal_to

from ..exception import (
    AuthenticationError,
    AuthenticationExpiredError,
    AuthServerUnavailableError,
    BusConnectionError,
    UnsupportedVersionError,
)
from ..protocol import CloseCode
from ..session import Session, _extract_version_from_path
from .helpers import run_async

_CONFIG = {'websocket': {'ping_interval': 30}}
_TOKEN = {
    'token': 'tok-id',
    'metadata': {'uuid': 'user-1', 'tenant_uuid': 'tenant-1'},
}


def _make_authenticator(**get_token_kwargs):
    authenticator = Mock()
    authenticator.get_token = AsyncMock(**get_token_kwargs)
    return authenticator


def _make_ws():
    ws = Mock()
    ws.close = AsyncMock()
    ws.send = AsyncMock()
    return ws


def _make_session(authenticator=None, bus_service=None, ws=None, token=_TOKEN):
    return Session(
        _CONFIG,
        authenticator or Mock(),
        bus_service or Mock(),
        Mock(),
        Mock(),
        ws or _make_ws(),
        '/',
        token=token,
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


def test_session_uses_injected_token_without_calling_auth():
    authenticator = _make_authenticator()
    bus_service = Mock()
    bus_service.create_consumer = AsyncMock(side_effect=BusConnectionError('stop'))
    session = _make_session(authenticator=authenticator, bus_service=bus_service)

    run_async(session.run())

    assert session.user_identity() == ('user-1', 'tenant-1')
    authenticator.get_token.assert_not_called()


def test_revoke_after_token_refresh_targets_refreshed_token():
    refreshed = {
        'token': 'tok-refreshed',
        'metadata': {'uuid': 'user-1', 'tenant_uuid': 'tenant-1'},
    }
    authenticator = _make_authenticator(return_value=refreshed)
    session = _make_session(authenticator=authenticator)
    session._consumer = Mock()

    run_async(session._do_ws_token(Mock(value='tok-refreshed')))
    with patch.object(
        session, '_run', AsyncMock(side_effect=AuthenticationExpiredError())
    ):
        run_async(session.run())

    authenticator.invalidate.assert_called_once_with('tok-refreshed')


def test_rejected_token_refresh_revokes_offered_token():
    authenticator = _make_authenticator(side_effect=AuthenticationError('revoked'))
    session = _make_session(authenticator=authenticator)
    session._consumer = Mock()

    with pytest.raises(AuthenticationError):
        run_async(session._do_ws_token(Mock(value='revoked-token')))

    authenticator.invalidate.assert_called_once_with('revoked-token')


def test_extract_version_defaults_to_1_when_absent():
    assert _extract_version_from_path('/') == 1


def test_extract_version_parses_supported_value():
    assert _extract_version_from_path('/?version=2') == 2


def test_extract_version_rejects_unsupported_value():
    with pytest.raises(UnsupportedVersionError):
        _extract_version_from_path('/?version=99')


def test_extract_version_rejects_non_numeric_value():
    with pytest.raises(UnsupportedVersionError):
        _extract_version_from_path('/?version=abc')


def test_auth_server_unavailable_does_not_invalidate_token():
    authenticator = _make_authenticator()
    ws = _make_ws()
    session = _make_session(authenticator=authenticator, ws=ws)

    with patch.object(
        session, '_run', AsyncMock(side_effect=AuthServerUnavailableError())
    ):
        run_async(session.run())

    authenticator.invalidate.assert_not_called()
    ws.close.assert_awaited_once_with(CloseCode.TRY_LATER, 'try again later')
