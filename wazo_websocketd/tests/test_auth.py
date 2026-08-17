# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import datetime
from unittest.mock import Mock, patch, sentinel

import pytest
import requests

from ..auth import (
    AsyncAuthClient,
    Authenticator,
    _DynamicIntervalAuthChecker,
    _StaticIntervalAuthChecker,
)
from ..exception import AuthenticationError, AuthenticationExpiredError


class TestAsyncAuthClient:
    _ACL = 'websocketd'

    @pytest.fixture(autouse=True)
    def auth_client(self):
        with patch('wazo_websocketd.auth.AuthClient') as factory:
            self.auth_client = factory.return_value
            self.client = AsyncAuthClient({'auth': {}})
            yield

    async def test_get_token(self):
        self.auth_client.token.get.return_value = sentinel.token

        token = await self.client.get_token(sentinel.token_id)

        assert token is sentinel.token
        self.auth_client.token.get.assert_called_once_with(sentinel.token_id, self._ACL)

    async def test_get_token_invalid(self):
        self.auth_client.token.get.side_effect = requests.HTTPError('403 Unauthorized')

        with pytest.raises(AuthenticationError):
            await self.client.get_token(sentinel.token_id)
        self.auth_client.token.get.assert_called_once_with(sentinel.token_id, self._ACL)

    async def test_is_valid_token(self):
        self.auth_client.token.is_valid.return_value = True

        assert await self.client.is_valid_token(sentinel.token_id) is True
        self.auth_client.token.is_valid.assert_called_once_with(
            sentinel.token_id, self._ACL
        )


class TestAuthenticator:
    @pytest.fixture(autouse=True)
    def clients(self):
        with patch('wazo_websocketd.auth.AsyncAuthClient') as async_factory, patch(
            'wazo_websocketd.auth.AuthClient'
        ):
            self.async_auth_client = async_factory.return_value
            self.authenticator = Authenticator(
                {'auth_check_static_interval': 1, 'auth_check_strategy': 'static'}
            )
            yield

    def test_get_token(self):
        coro = self.authenticator.get_token(sentinel.token_id)

        assert coro is self.async_auth_client.get_token.return_value
        self.async_auth_client.get_token.assert_called_once_with(sentinel.token_id)

    def test_is_valid_token(self):
        coro = self.authenticator.is_valid_token(sentinel.token_id, sentinel.acl)

        assert coro is self.async_auth_client.is_valid_token.return_value
        self.async_auth_client.is_valid_token.assert_called_once_with(
            sentinel.token_id, sentinel.acl
        )


class TestStaticIntervalAuthChecker:
    async def test_the_session_ends_once_the_token_stops_validating(self):
        client = Mock()

        async def is_valid_token(_token_id):
            return False

        client.is_valid_token = is_valid_token
        check = _StaticIntervalAuthChecker(client, {'auth_check_static_interval': 0.1})

        with pytest.raises(AuthenticationExpiredError):
            await check.run(lambda: {'token': sentinel.token_id})


class TestDynamicIntervalAuthChecker:
    @pytest.mark.parametrize(
        ('expires_in', 'next_check'),
        [
            pytest.param(datetime.timedelta(seconds=-10), 15, id='already expired'),
            pytest.param(datetime.timedelta(seconds=4), 4, id='expiring in seconds'),
            pytest.param(datetime.timedelta(seconds=10), 10, id='expiring soon'),
            pytest.param(datetime.timedelta(seconds=70), 60, id='under 80s'),
            pytest.param(datetime.timedelta(seconds=100), 75, id='over 80s'),
            pytest.param(datetime.timedelta(days=1), 43200, id='over a day'),
        ],
    )
    def test_the_next_check_is_scheduled_from_the_expiry(self, expires_in, next_check):
        check = _DynamicIntervalAuthChecker(Mock(), {})
        now = datetime.datetime(2016, 1, 1, 0, 1, 0)

        assert check._calculate_next_check(now, now + expires_in) == next_check
