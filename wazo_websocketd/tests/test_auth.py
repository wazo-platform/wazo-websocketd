# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import datetime
import unittest
from unittest.mock import Mock, sentinel, patch

import requests
from hamcrest import assert_that, equal_to, same_instance

from ..auth import (
    Authenticator,
    AsyncAuthClient,
    _DynamicIntervalAuthCheck,
    _StaticIntervalAuthCheck,
)
from ..exception import AuthenticationError, AuthenticationExpiredError


class TestWebSocketdAuthClient(unittest.TestCase):
    _ACL = 'websocketd'

    def setUp(self):
        self.auth_client = Mock()
        patcher = patch(
            "wazo_websocketd.auth.AuthClient",
            return_value=self.auth_client,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.websocketd_auth_client = AsyncAuthClient({"auth": {}})

    def test_get_token(self):
        self.auth_client.token.get.return_value = sentinel.token

        token = asyncio.get_event_loop().run_until_complete(
            self.websocketd_auth_client.get_token(sentinel.token_id)
        )

        assert_that(token, same_instance(sentinel.token))
        self.auth_client.token.get.assert_called_once_with(sentinel.token_id, self._ACL)

    def test_get_token_invalid(self):
        self.auth_client.token.get.side_effect = requests.HTTPError('403 Unauthorized')

        self.assertRaises(
            AuthenticationError,
            asyncio.get_event_loop().run_until_complete,
            self.websocketd_auth_client.get_token(sentinel.token_id),
        )
        self.auth_client.token.get.assert_called_once_with(sentinel.token_id, self._ACL)

    def test_is_valid_token(self):
        self.auth_client.token.is_valid.return_value = True

        is_valid = asyncio.get_event_loop().run_until_complete(
            self.websocketd_auth_client.is_valid_token(sentinel.token_id)
        )

        assert_that(is_valid)
        self.auth_client.token.is_valid.assert_called_once_with(
            sentinel.token_id, self._ACL
        )


class TestAuthenticator(unittest.TestCase):
    def setUp(self):
        self.websocketd_auth_client = Mock()
        patcher = patch(
            "wazo_websocketd.auth.AsyncAuthClient",
            return_value=self.websocketd_auth_client,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.auth_client = Mock()
        patcher = patch(
            "wazo_websocketd.auth.AuthClient",
            return_value=self.auth_client,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.authenticator = Authenticator(
            {"auth_check_static_interval": 1, "auth_check_strategy": "static"}
        )

    def test_get_token(self):
        coro = self.authenticator.get_token(sentinel.token_id)

        assert_that(
            coro, same_instance(self.websocketd_auth_client.get_token.return_value)
        )
        self.websocketd_auth_client.get_token.assert_called_once_with(sentinel.token_id)

    def test_is_valid_token(self):
        coro = self.authenticator.is_valid_token(sentinel.token_id, sentinel.acl)

        assert_that(
            coro, same_instance(self.websocketd_auth_client.is_valid_token.return_value)
        )
        self.websocketd_auth_client.is_valid_token.assert_called_once_with(
            sentinel.token_id, sentinel.acl
        )


class TestStaticIntervalAuthCheck(unittest.TestCase):
    def setUp(self):
        self.websocketd_auth_client = Mock()
        self.check = _StaticIntervalAuthCheck(
            self.websocketd_auth_client, {"auth_check_static_interval": 0.1}
        )
        self.token = {'token': sentinel.token_id}

    def test_run(self):
        async def is_valid_token(token_id):
            return False

        self.websocketd_auth_client.is_valid_token = is_valid_token

        self.assertRaises(
            AuthenticationExpiredError,
            asyncio.get_event_loop().run_until_complete,
            self.check.run(lambda: self.token),
        )


class TestDynamicIntervalAuthCheck(unittest.TestCase):
    def setUp(self):
        self.websocketd_auth_client = Mock()
        self.check = _DynamicIntervalAuthCheck(self.websocketd_auth_client, {})

    def test_expiration_in_the_past(self):
        now = datetime.datetime(2016, 1, 1, 0, 0, 0)
        expires_at = now - datetime.timedelta(seconds=10)

        result = self.check._calculate_next_check(now, expires_at)

        assert_that(result, equal_to(15))

    def test_expiration_less_than_80_seconds(self):
        now = datetime.datetime(2016, 1, 1, 0, 0, 0)
        expires_at = now + datetime.timedelta(seconds=10)

        result = self.check._calculate_next_check(now, expires_at)

        assert_that(result, equal_to(60))

    def test_expiration_more_than_75_seconds(self):
        now = datetime.datetime(2016, 1, 1, 0, 1, 0)
        expires_at = now + datetime.timedelta(seconds=100)

        result = self.check._calculate_next_check(now, expires_at)

        assert_that(result, equal_to(75))

    def test_expiration_more_than_1_day(self):
        now = datetime.datetime(2016, 1, 1, 0, 1, 0)
        expires_at = now + datetime.timedelta(days=1)

        result = self.check._calculate_next_check(now, expires_at)

        assert_that(result, equal_to(43200))
