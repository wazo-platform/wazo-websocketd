# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import time
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

import pytest

from ..exception import AuthenticationError, AuthServerUnavailableError
from ..helpers.timing import utcnow_naive
from ..token_cache import CachingAuthenticator, _token_size, mask_uuid
from .helpers import Clock

_TOKEN = {
    'token': 'tok',
    'metadata': {'uuid': 'u', 'tenant_uuid': 't'},
    'utc_expires_at': '2999-01-01T00:00:00',
}


def _session_token(session_uuid, token_id='T'):
    # like wazo-auth's Token.to_dict, session_uuid is a top-level field
    return {
        'token': token_id,
        'session_uuid': session_uuid,
        'metadata': {'uuid': 'u', 'tenant_uuid': 't'},
        'utc_expires_at': '2999-01-01T00:00:00',
    }


def _authenticator(**get_token_kwargs):
    authenticator = Mock()
    authenticator.get_token = AsyncMock(**get_token_kwargs)
    return authenticator


def _caching(
    authenticator,
    *,
    positive_ttl=10.0,
    negative_ttl=60.0,
    max_size=16 * 1024 * 1024,
    max_negative_entries=10000,
    timer=time.monotonic,
):
    return CachingAuthenticator(
        authenticator,
        positive_ttl=positive_ttl,
        negative_ttl=negative_ttl,
        max_size=max_size,
        max_negative_entries=max_negative_entries,
        timer=timer,
    )


class TestCaching:
    async def test_a_validated_token_is_served_from_the_cache(self):
        authenticator = _authenticator(return_value=_TOKEN)
        cache = _caching(authenticator)

        assert await cache.get_token('T') == _TOKEN
        assert await cache.get_token('T') == _TOKEN
        assert authenticator.get_token.await_count == 1

    async def test_the_cache_is_keyed_by_token_and_scope(self):
        authenticator = _authenticator(return_value=_TOKEN)
        cache = _caching(authenticator)

        await cache.get_token('T', 'websocketd')
        await cache.get_token('T', 'chatd')
        await cache.get_token('T', 'websocketd')

        assert authenticator.get_token.await_count == 2
        authenticator.get_token.assert_any_await('T', 'websocketd')
        authenticator.get_token.assert_any_await('T', 'chatd')

    async def test_an_entry_expires_with_its_ttl(self):
        authenticator = _authenticator(return_value=_TOKEN)
        clock = Clock(1000.0)
        cache = _caching(authenticator, positive_ttl=30, timer=clock)

        await cache.get_token('T')
        clock.now = 1060.0
        await cache.get_token('T')

        assert authenticator.get_token.await_count == 2

    async def test_an_entry_never_outlives_the_token_it_holds(self):
        expires_in = 2.0
        token = dict(
            _TOKEN, utc_expires_at=str(utcnow_naive() + timedelta(seconds=expires_in))
        )
        authenticator = _authenticator(return_value=token)
        clock = Clock(1000.0)
        cache = _caching(authenticator, positive_ttl=300, timer=clock)

        await cache.get_token('T')
        clock.now += expires_in + 1.0
        await cache.get_token('T')

        # this is what lets the dynamic auth check trust the cache: it re-checks
        # near expiry, and a stale entry can never answer that check
        assert authenticator.get_token.await_count == 2

    async def test_an_already_expired_token_is_served_but_not_kept(self):
        token = {**_TOKEN, 'utc_expires_at': '2000-01-01T00:00:00'}
        authenticator = _authenticator(return_value=token)
        cache = _caching(authenticator)

        assert await cache.get_token('T') == token
        assert await cache.get_token('T') == token
        assert cache.positive_entries() == 0
        assert authenticator.get_token.await_count == 2

    async def test_a_timezone_aware_expiry_is_understood(self):
        token = {**_TOKEN, 'utc_expires_at': '2999-01-01T00:00:00+00:00'}
        cache = _caching(_authenticator(return_value=token))

        assert await cache.get_token('T') == token
        assert cache.positive_entries() == 1


class TestRejections:
    async def test_a_rejection_is_cached(self):
        authenticator = _authenticator(side_effect=AuthenticationError('bad'))
        cache = _caching(authenticator)

        for _ in range(2):
            with pytest.raises(AuthenticationError):
                await cache.get_token('T')

        assert authenticator.get_token.await_count == 1

    async def test_a_rejection_keeps_the_status_it_came_with(self):
        authenticator = _authenticator(
            side_effect=AuthenticationError('unknown token', status_code=404)
        )
        cache = _caching(authenticator)

        statuses = []
        for _ in range(2):
            with pytest.raises(AuthenticationError) as refused:
                await cache.get_token('T')
            statuses.append(refused.value.status_code)

        assert statuses == [404, 404]
        assert authenticator.get_token.await_count == 1

    async def test_a_rejection_is_only_cached_briefly(self):
        authenticator = _authenticator(
            side_effect=AuthenticationError('unknown token', status_code=404)
        )
        clock = Clock(1000.0)
        cache = _caching(authenticator, timer=clock)

        for _ in range(2):
            with pytest.raises(AuthenticationError):
                await cache.get_token('T')
        clock.now += CachingAuthenticator.NEGATIVE_TTL_CEILING + 1
        with pytest.raises(AuthenticationError):
            await cache.get_token('T')

        # wazo-auth can start accepting a token it just refused
        assert authenticator.get_token.await_count == 2

    async def test_a_token_wazo_auth_starts_accepting_is_served_again(self):
        outcomes = [AuthenticationError('not replicated yet', status_code=404), _TOKEN]
        authenticator = Mock()

        async def get_token(_token_id, _acl=None):
            outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        authenticator.get_token = AsyncMock(side_effect=get_token)
        clock = Clock(1000.0)
        cache = _caching(authenticator, timer=clock)

        with pytest.raises(AuthenticationError):
            await cache.get_token('T')
        clock.now += CachingAuthenticator.NEGATIVE_TTL_CEILING + 1

        assert await cache.get_token('T') == _TOKEN

    async def test_an_outage_is_never_cached_as_a_rejection(self):
        authenticator = _authenticator(side_effect=AuthServerUnavailableError('down'))
        cache = _caching(authenticator)

        for _ in range(2):
            with pytest.raises(AuthServerUnavailableError):
                await cache.get_token('T')

        assert authenticator.get_token.await_count == 2


class TestSingleFlight:
    async def test_concurrent_lookups_share_one_request(self):
        release = asyncio.Event()
        calls = 0

        async def slow_get_token(_token_id, _acl=None):
            nonlocal calls
            calls += 1
            await release.wait()
            return _TOKEN

        authenticator = Mock()
        authenticator.get_token = slow_get_token
        cache = _caching(authenticator)

        first = asyncio.create_task(cache.get_token('T'))
        second = asyncio.create_task(cache.get_token('T'))
        await asyncio.sleep(0.01)  # let both reach the in-flight lookup
        release.set()

        assert await asyncio.gather(first, second) == [_TOKEN, _TOKEN]
        assert calls == 1

    async def test_a_waiter_giving_up_does_not_cancel_the_shared_request(self):
        release = asyncio.Event()
        calls = 0

        async def slow_get_token(_token_id, _acl=None):
            nonlocal calls
            calls += 1
            await release.wait()
            return _TOKEN

        authenticator = Mock()
        authenticator.get_token = slow_get_token
        cache = _caching(authenticator)

        leaving = asyncio.create_task(cache.get_token('T'))
        staying = asyncio.create_task(cache.get_token('T'))
        await asyncio.sleep(0.01)  # both waiting on the same lookup

        leaving.cancel()
        await asyncio.sleep(0.01)
        release.set()

        assert await staying == _TOKEN
        assert calls == 1


class TestInFlightBudget:
    async def test_a_flood_of_unknown_tokens_is_refused_rather_than_relayed(self):
        release = asyncio.Event()
        started = 0

        async def slow_get_token(_token_id, _acl=None):
            nonlocal started
            started += 1
            await release.wait()
            return _TOKEN

        authenticator = Mock()
        authenticator.get_token = slow_get_token
        cache = _caching(authenticator)

        with patch.object(CachingAuthenticator, 'MAX_INFLIGHT_LOOKUPS', 2):
            accepted = [asyncio.create_task(cache.get_token(t)) for t in 'AB']
            await asyncio.sleep(0.01)  # both lookups are now in flight
            with pytest.raises(AuthServerUnavailableError):
                await asyncio.wait_for(cache.get_token('C'), 0.5)
            release.set()
            result = await asyncio.gather(*accepted)

        assert result == [_TOKEN, _TOKEN]
        assert started == 2  # the refused lookup never reached wazo-auth

    async def test_stopping_cancels_the_lookups_still_in_flight(self):
        reached_upstream = asyncio.Event()

        async def hanging_get_token(_token_id, _acl=None):
            reached_upstream.set()
            await asyncio.Event().wait()

        authenticator = Mock()
        authenticator.get_token = hanging_get_token
        cache = _caching(authenticator)

        waiter = asyncio.create_task(cache.get_token('T'))
        await reached_upstream.wait()
        await cache.stop()

        assert cache.inflight_lookups() == 0
        with pytest.raises(asyncio.CancelledError):
            await waiter


class TestSessionEviction:
    async def test_a_deleted_session_takes_its_tokens_with_it(self):
        tokens = {
            'A': _session_token('session-a', token_id='A'),
            'B': _session_token('session-b', token_id='B'),
        }
        authenticator = Mock()
        authenticator.get_token = AsyncMock(side_effect=lambda t, acl=None: tokens[t])
        cache = _caching(authenticator)

        await cache.get_token('A')
        await cache.get_token('B')
        cache.evict_session('session-a')

        # the token is dead, so re-entry is refused here rather than upstream
        with pytest.raises(AuthenticationError) as refused:
            await cache.get_token('A')
        await cache.get_token('B')  # untouched: still cached

        assert refused.value.status_code == 404
        assert authenticator.get_token.await_count == 2

    async def test_the_refusal_expires_so_wazo_auth_stays_the_authority(self):
        authenticator = _authenticator(
            return_value=_session_token('session-a', token_id='A')
        )
        clock = Clock(1000.0)
        cache = _caching(authenticator, negative_ttl=5.0, timer=clock)

        await cache.get_token('A')
        cache.evict_session('session-a')
        clock.now += 6.0
        await cache.get_token('A')

        assert authenticator.get_token.await_count == 2

    async def test_deleting_an_uncached_session_changes_nothing(self):
        cache = _caching(_authenticator(return_value=_TOKEN))

        cache.evict_session('unknown-session')

        assert await cache.get_token('T') == _TOKEN

    async def test_a_lookup_in_flight_when_the_deletion_arrives_is_refused(self):
        release = asyncio.Event()

        async def slow_upstream(_token_id, _acl=None):
            # wazo-auth still answers 200: it has not committed the delete yet
            await release.wait()
            return _session_token('session-a', token_id='A')

        cache = _caching(_authenticator(side_effect=slow_upstream))

        lookup = asyncio.ensure_future(cache.get_token('A'))
        await asyncio.sleep(0)
        cache.evict_session('session-a')
        release.set()

        with pytest.raises(AuthenticationError) as refused:
            await lookup

        assert refused.value.status_code == 404
        assert cache.positive_entries() == 0

    async def test_a_session_deleted_long_ago_no_longer_taints_new_tokens(self):
        clock = Clock(1000.0)
        token = _session_token('session-a', token_id='A')
        cache = _caching(_authenticator(return_value=token), timer=clock)

        cache.evict_session('session-a')
        clock.now += CachingAuthenticator.DELETED_SESSION_MEMORY + 1.0

        # wazo-auth is the authority again: it would answer 404 itself by now
        assert await cache.get_token('A') == token

    async def test_eviction_does_not_walk_the_whole_cache(self):
        tokens = {
            f'T{i}': _session_token(f'session-{i}', token_id=f'T{i}')
            for i in range(500)
        }
        authenticator = Mock()
        authenticator.get_token = AsyncMock(side_effect=lambda t, acl=None: tokens[t])
        cache = _caching(authenticator)

        for token_id in tokens:
            await cache.get_token(token_id)

        walked = 0
        original = type(cache._positive).__getitem__

        def counting(self, key):
            nonlocal walked
            walked += 1
            return original(self, key)

        with patch.object(type(cache._positive), '__getitem__', counting):
            cache.evict_session('session-7')

        # the deleted session's own entry, not one lookup per cached token
        assert walked <= 2
        assert cache.positive_entries() == len(tokens) - 1

    async def test_the_session_index_does_not_outgrow_the_cache(self):
        clock = Clock(1000.0)
        tokens = {
            f'T{i}': _session_token(f'session-{i}', token_id=f'T{i}')
            for i in range(100)
        }
        authenticator = Mock()
        authenticator.get_token = AsyncMock(side_effect=lambda t, acl=None: tokens[t])
        cache = _caching(authenticator, positive_ttl=10.0, timer=clock)
        first_half, second_half = list(tokens)[:50], list(tokens)[50:]

        for token_id in first_half:
            await cache.get_token(token_id)
        clock.now += 11.0  # they expire on their own, telling the index nothing
        for token_id in second_half:
            await cache.get_token(token_id)

        assert cache.positive_entries() == len(second_half)
        assert len(cache._by_session) == cache.positive_entries()


class TestDegradedMode:
    async def test_new_entries_are_clamped_while_eviction_is_lost(self):
        authenticator = _authenticator(return_value=_TOKEN)
        clock = Clock(1000.0)
        cache = _caching(authenticator, positive_ttl=300, timer=clock)

        cache.eviction_lost()
        await cache.get_token('T')
        clock.now += CachingAuthenticator.DEGRADED_TTL + 1
        await cache.get_token('T')  # degraded entry expired: refetch
        cache.eviction_active()
        await cache.get_token('T')  # fresh entry gets the full ttl again
        clock.now += CachingAuthenticator.DEGRADED_TTL + 1
        await cache.get_token('T')  # still cached

        assert authenticator.get_token.await_count == 3

    async def test_entries_cached_before_the_loss_are_clamped_too(self):
        authenticator = _authenticator(return_value=_TOKEN)
        clock = Clock(1000.0)
        cache = _caching(authenticator, positive_ttl=300, timer=clock)

        await cache.get_token('T')  # cached with the full 300s ttl
        cache.eviction_lost()  # staleness must be bounded
        clock.now += CachingAuthenticator.DEGRADED_TTL + 1
        await cache.get_token('T')

        assert authenticator.get_token.await_count == 2

    async def test_recovering_clears_tokens_but_keeps_rejections(self):
        authenticator = Mock()

        async def get_token(token_id, _acl=None):
            if token_id == 'bad':
                raise AuthenticationError('nope', status_code=404)
            return _session_token('s', token_id='good')

        authenticator.get_token = AsyncMock(side_effect=get_token)
        cache = _caching(authenticator)

        await cache.get_token('good')
        with pytest.raises(AuthenticationError):
            await cache.get_token('bad')

        cache.eviction_lost()

        await cache.get_token('good')  # refetched
        with pytest.raises(AuthenticationError):
            await cache.get_token('bad')  # still refused from the cache

        assert authenticator.get_token.await_count == 3


class TestCacheBudget:
    async def test_the_cache_is_bounded_by_size(self):
        authenticator = _authenticator(return_value=_TOKEN)
        budget = 2 * _token_size(_TOKEN)
        cache = _caching(authenticator, max_size=budget)

        for i in range(5):
            await cache.get_token(f'T{i}')

        assert cache.positive_entries() == 2
        assert cache._positive.currsize <= budget

    async def test_a_token_larger_than_the_budget_is_served_but_not_kept(self):
        cache = _caching(_authenticator(return_value=_TOKEN), max_size=1)

        assert await cache.get_token('T') == _TOKEN
        assert cache.positive_entries() == 0

    def test_a_token_size_grows_with_its_acls(self):
        small = {'token': 't', 'metadata': {}, 'acl': []}
        large = {'token': 't', 'metadata': {}, 'acl': ['dird.#'] * 50}

        assert _token_size(large) > _token_size(small)


class TestMasking:
    def test_a_masked_uuid_keeps_its_shape_and_last_8(self):
        uuid = UUID('00000000-0000-4000-a000-0123456789ab')
        expected = 'XXXXXXXX-XXXX-XXXX-XXXX-XXXX456789ab'

        assert mask_uuid(str(uuid)) == expected
        assert mask_uuid(uuid) == expected
