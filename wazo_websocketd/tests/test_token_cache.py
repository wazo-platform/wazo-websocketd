# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from unittest.mock import AsyncMock, Mock
from uuid import UUID

from ..exception import AuthenticationError, AuthServerUnavailableError
from ..token_cache import CachingAuthenticator, _token_size, mask_uuid
from .helpers import run_async

_TOKEN = {
    'token': 'tok',
    'metadata': {'uuid': 'u', 'tenant_uuid': 't'},
    'utc_expires_at': '2999-01-01T00:00:00',
}


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _authenticator(**get_token_kwargs):
    authenticator = Mock()
    authenticator.get_token = AsyncMock(**get_token_kwargs)
    return authenticator


def test_positive_result_is_cached():
    authenticator = _authenticator(return_value=_TOKEN)
    cache = CachingAuthenticator(authenticator)

    async def scenario():
        return await cache.get_token('T'), await cache.get_token('T')

    first, second = run_async(scenario())
    assert first == _TOKEN
    assert second == _TOKEN
    assert authenticator.get_token.await_count == 1


def test_hard_rejection_is_cached():
    authenticator = _authenticator(side_effect=AuthenticationError('bad'))
    cache = CachingAuthenticator(authenticator)

    async def scenario():
        for _ in range(2):
            try:
                await cache.get_token('T')
            except AuthenticationError:
                pass

    run_async(scenario())
    assert authenticator.get_token.await_count == 1


def test_soft_failure_is_not_cached():
    authenticator = _authenticator(side_effect=AuthServerUnavailableError('down'))
    cache = CachingAuthenticator(authenticator)

    async def scenario():
        for _ in range(2):
            try:
                await cache.get_token('T')
            except AuthServerUnavailableError:
                pass

    run_async(scenario())
    assert authenticator.get_token.await_count == 2


def test_single_flight_coalesces_concurrent_lookups():
    calls = 0
    release = asyncio.Event()

    async def slow_get_token(token_id):
        nonlocal calls
        calls += 1
        await release.wait()
        return _TOKEN

    authenticator = Mock()
    authenticator.get_token = slow_get_token
    cache = CachingAuthenticator(authenticator)

    async def scenario():
        first = asyncio.create_task(cache.get_token('T'))
        second = asyncio.create_task(cache.get_token('T'))
        await asyncio.sleep(0.01)  # let both reach the in-flight lookup
        release.set()
        return await asyncio.gather(first, second)

    assert run_async(scenario()) == [_TOKEN, _TOKEN]
    assert calls == 1


def test_invalidate_evicts_positive_and_poisons_negative():
    authenticator = _authenticator(return_value=_TOKEN)
    cache = CachingAuthenticator(authenticator)

    async def scenario():
        await cache.get_token('T')  # positively cached
        cache.invalidate('T')
        try:
            await cache.get_token('T')  # now a negative hit
            return 'admitted'
        except AuthenticationError:
            return 'rejected'

    assert run_async(scenario()) == 'rejected'
    assert authenticator.get_token.await_count == 1


def test_tz_aware_expiry_is_cached_without_error():
    token = {**_TOKEN, 'utc_expires_at': '2999-01-01T00:00:00+00:00'}
    authenticator = _authenticator(return_value=token)
    cache = CachingAuthenticator(authenticator)

    async def scenario():
        return await cache.get_token('T'), len(cache._positive)

    result, count = run_async(scenario())
    assert result == token
    assert count == 1


def test_positive_entry_expires():
    authenticator = _authenticator(return_value=_TOKEN)
    clock = _Clock(1000.0)
    cache = CachingAuthenticator(authenticator, positive_ttl=30, timer=clock)

    async def scenario():
        await cache.get_token('T')
        clock.now = 1060.0
        await cache.get_token('T')

    run_async(scenario())
    assert authenticator.get_token.await_count == 2


def test_positive_cache_is_bounded_by_size():
    authenticator = _authenticator(return_value=_TOKEN)
    budget = 2 * _token_size(_TOKEN)
    cache = CachingAuthenticator(authenticator, max_size=budget)

    async def scenario():
        for i in range(5):
            await cache.get_token(f'T{i}')
        return len(cache._positive), cache._positive.currsize

    count, currsize = run_async(scenario())
    assert count == 2
    assert currsize <= budget


def test_token_size_grows_with_acls():
    small = {'token': 't', 'metadata': {}, 'acl': []}
    large = {'token': 't', 'metadata': {}, 'acl': ['dird.#'] * 50}
    assert _token_size(large) > _token_size(small)


def test_token_larger_than_budget_is_returned_but_not_cached():
    authenticator = _authenticator(return_value=_TOKEN)
    cache = CachingAuthenticator(authenticator, max_size=1)

    async def scenario():
        return await cache.get_token('T'), len(cache._positive)

    token, count = run_async(scenario())
    assert token == _TOKEN
    assert count == 0


def test_mask_keeps_uuid_shape_and_last_8():
    uuid = UUID('00000000-0000-4000-a000-0123456789ab')
    expected = 'XXXXXXXX-XXXX-XXXX-XXXX-XXXX456789ab'
    assert mask_uuid(str(uuid)) == expected
    assert mask_uuid(uuid) == expected
