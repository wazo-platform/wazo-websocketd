# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import datetime
from typing import cast
from unittest.mock import Mock

import aiohttp
import pytest

from ..auth import (
    AsyncAuthClient,
    Authenticator,
    _AuthChecker,
    _DynamicIntervalAuthChecker,
    _StaticIntervalAuthChecker,
    build_authenticator,
)
from ..config import _DEFAULT_CONFIG
from ..exception import (
    AuthenticationError,
    AuthenticationExpiredError,
    AuthServerUnavailableError,
)
from .helpers import auth_client, fake_wazo_auth, refused_config

_SECRET = 'ac6b1c8e-0000-4000-8000-secrettokenid'


def _valid_token(token_id='T', expires_in=3600):
    expires_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=expires_in)
    return {
        'token': token_id,
        'metadata': {'uuid': 'u', 'tenant_uuid': 't'},
        'utc_expires_at': expires_at.isoformat(),
    }


class TestAsyncAuthClient:
    async def test_get_token_returns_the_validated_token(self):
        token = _valid_token()

        async with fake_wazo_auth() as fake:
            fake.tokens['T'] = token
            async with auth_client({'auth': fake.config()}) as client:
                assert await client.get_token('T') == token

            assert fake.requests == [('GET', 'T', 'websocketd')]

    async def test_is_valid_token(self):
        async with fake_wazo_auth() as fake:
            fake.tokens['T'] = _valid_token()
            async with auth_client({'auth': fake.config()}) as client:
                assert await client.is_valid_token('T') is True

            assert fake.requests == [('HEAD', 'T', 'websocketd')]

    @pytest.mark.parametrize(
        'status',
        [
            pytest.param(400, id='bad request'),
            pytest.param(401, id='unauthorized'),
            pytest.param(403, id='forbidden'),
            pytest.param(404, id='unknown token'),
        ],
    )
    async def test_a_client_error_is_a_rejection_carrying_its_status(self, status):
        async with fake_wazo_auth() as fake:
            fake.statuses['T'] = status
            async with auth_client({'auth': fake.config()}) as client:
                with pytest.raises(AuthenticationError) as refused:
                    await client.get_token('T')

        # retrying cannot make wazo-auth like this request any better
        assert refused.value.status_code == status

    async def test_a_server_error_is_an_outage_not_a_rejection(self):
        async with fake_wazo_auth() as fake:
            fake.statuses['T'] = 500
            async with auth_client({'auth': fake.config()}) as client:
                with pytest.raises(AuthServerUnavailableError):
                    await client.get_token('T')

    async def test_an_unreachable_server_is_an_outage(self):
        async with auth_client({'auth': refused_config()}) as client:
            with pytest.raises(AuthServerUnavailableError):
                await client.get_token('T')
            with pytest.raises(AuthServerUnavailableError):
                await client.is_valid_token('T')

    async def test_create_token_posts_to_wazo_auth_with_its_credentials(self):
        async with fake_wazo_auth() as fake:
            config = {
                'auth': {**fake.config(), 'username': 'svc', 'password': 'secret'}
            }
            async with auth_client(config) as client:
                assert await client.create_token(60) == {'token': 'service-token'}

            body, authorization = fake.created[0]

        assert body == {'expiration': 60}
        assert authorization == aiohttp.BasicAuth('svc', 'secret').encode()


class TestTokenRedaction:
    @pytest.mark.parametrize(
        ('status', 'error'),
        [
            pytest.param(401, AuthenticationError, id='rejection'),
            pytest.param(503, AuthServerUnavailableError, id='outage'),
        ],
    )
    async def test_get_token_errors_do_not_carry_the_token(self, status, error):
        async with fake_wazo_auth() as fake:
            fake.statuses[_SECRET] = status
            async with auth_client({'auth': fake.config()}) as client:
                with pytest.raises(error) as raised:
                    await client.get_token(_SECRET)

        # the url carries the token id, so the client error cannot be logged as is
        assert _SECRET not in str(raised.value)
        assert str(status) in str(raised.value)

    async def test_is_valid_token_errors_do_not_carry_the_token(self):
        async with fake_wazo_auth() as fake:
            fake.statuses[_SECRET] = 503
            async with auth_client({'auth': fake.config()}) as client:
                with pytest.raises(AuthServerUnavailableError) as raised:
                    await client.is_valid_token(_SECRET)

        assert _SECRET not in str(raised.value)


class TestAuthCheckStrategy:
    def test_an_unknown_strategy_is_refused_by_name(self):
        with pytest.raises(ValueError) as refused:
            _AuthChecker.get_strategy('made-up')

        assert 'made-up' in str(refused.value)

    @pytest.mark.parametrize(
        ('strategy', 'expected'),
        [
            pytest.param('static', _StaticIntervalAuthChecker, id='static'),
            pytest.param('dynamic', _DynamicIntervalAuthChecker, id='dynamic'),
        ],
    )
    def test_the_configured_strategy_is_the_one_built(self, strategy, expected):
        config = {
            'auth': {'host': '127.0.0.1', 'port': 80, 'https': False},
            'auth_check_strategy': strategy,
            'auth_check_static_interval': 60,
            'auth_check_max_unavailable': 5,
        }

        authenticator = build_authenticator(config)

        assert isinstance(authenticator, Authenticator)
        assert isinstance(authenticator._auth_check, expected)


class TestStaticIntervalAuthChecker:
    async def test_the_session_ends_once_the_token_stops_validating(self):
        client = Mock()

        async def is_valid_token(_token_id):
            return False

        client.is_valid_token = is_valid_token
        check = _StaticIntervalAuthChecker(
            client,
            {'auth_check_static_interval': 0.01, 'auth_check_max_unavailable': 5},
        )

        with pytest.raises(AuthenticationExpiredError):
            await check.run(lambda: {'token': 'T'})


class TestUnavailableTolerance:
    def _checker(self, client, max_unavailable):
        return _StaticIntervalAuthChecker(
            client,
            {
                'auth_check_static_interval': 0.01,
                'auth_check_max_unavailable': max_unavailable,
            },
        )

    async def test_a_brief_outage_does_not_end_the_session(self):
        client = Mock()
        checks = 0

        async def is_valid_token(_token_id):
            nonlocal checks
            checks += 1
            if checks == 1:
                raise AuthServerUnavailableError()
            return True

        client.is_valid_token = is_valid_token
        check = self._checker(client, max_unavailable=5)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(check.run(lambda: {'token': 'T'}), timeout=0.1)

    async def test_an_outage_that_outlasts_the_tolerance_ends_the_session(self):
        client = Mock()

        async def is_valid_token(_token_id):
            raise AuthServerUnavailableError()

        client.is_valid_token = is_valid_token
        check = self._checker(client, max_unavailable=2)

        with pytest.raises(AuthServerUnavailableError):
            await check.run(lambda: {'token': 'T'})


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
        check = _DynamicIntervalAuthChecker(Mock(), {'auth_check_max_unavailable': 5})
        now = datetime.datetime(2016, 1, 1, 0, 1, 0)

        assert check._calculate_next_check(now, now + expires_in) == next_check

    async def test_a_rejected_token_ends_the_session(self):
        client = Mock()

        async def get_token(_token_id):
            raise AuthenticationError('gone', status_code=404)

        client.get_token = get_token
        check = _DynamicIntervalAuthChecker(client, {'auth_check_max_unavailable': 5})

        with pytest.raises(AuthenticationExpiredError):
            await check.run(
                lambda: {'token': 'T', 'utc_expires_at': '2016-01-01T00:00:00'}
            )


class TestEndpointAuthority:
    @pytest.mark.parametrize(
        'host, expected',
        [
            ('auth.example.com', 'https://auth.example.com:9497/0.1'),
            ('::1', 'https://[::1]:9497/0.1'),
            ('[::1]', 'https://[::1]:9497/0.1'),
        ],
    )
    def test_an_ipv6_literal_is_bracketed_as_a_url_authority_requires(
        self, host, expected
    ):
        client = AsyncAuthClient(
            {'host': host, 'port': 9497, 'prefix': None, 'https': True}
        )

        assert client._base_url == expected


class TestDefaultEndpoint:
    def test_the_shipped_config_reaches_wazo_auth_behind_nginx(self):
        client = AsyncAuthClient(cast(dict, _DEFAULT_CONFIG['auth']))

        # port 80 is nginx, which routes this prefix to wazo-auth
        assert client._base_url == 'http://localhost:80/api/auth/0.1'

    def test_a_prefix_set_to_null_reaches_wazo_auth_directly(self):
        client = AsyncAuthClient({'host': 'auth', 'port': 9497, 'prefix': None})

        assert client._base_url == 'http://auth:9497/0.1'
