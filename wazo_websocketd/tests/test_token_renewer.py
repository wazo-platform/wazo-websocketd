# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest

from ..token_renewer import ServiceTokenRenewer

_TOKEN = {
    'token': 'tok',
    'metadata': {'uuid': 'u', 'tenant_uuid': 't'},
    'utc_expires_at': '2999-01-01T00:00:00',
}


class TestServiceTokenRenewer:
    @pytest.fixture(autouse=True)
    def auth_client(self):
        with patch('wazo_websocketd.token_renewer.AsyncAuthClient') as factory:
            self.client = factory.return_value
            self.client.create_token = AsyncMock(return_value=_TOKEN)
            self.client.close = AsyncMock()
            self.renewer = ServiceTokenRenewer({'auth': {}})
            yield

    async def test_a_subscriber_can_retire_the_renewer(self):
        async with self.renewer:
            self.renewer.subscribe(Mock(), details=True, oneshot=True)
            self.renewer.subscribe(self.renewer.stop, oneshot=True)
            await asyncio.sleep(0.05)

            # nothing reads the token once the master tenant is known, and it
            # never changes at runtime, so renewing only churns wazo-auth sessions
            assert self.renewer._task.done() is True

        assert self.client.create_token.call_count == 1

    async def test_subscribers_are_served_before_the_renewer_is_retired(self):
        served = Mock()

        async with self.renewer:
            self.renewer.subscribe(served, details=True, oneshot=True)
            self.renewer.subscribe(self.renewer.stop, oneshot=True)
            await asyncio.sleep(0.05)

        served.assert_called_once_with(_TOKEN)

    async def test_a_failure_to_mint_a_token_says_why(self, caplog):
        self.client.create_token = AsyncMock(
            side_effect=RuntimeError('wazo-auth refused the service credentials')
        )

        with caplog.at_level(logging.ERROR):
            async with self.renewer:
                await asyncio.sleep(0)  # let the first attempt fail

        # without the cause, a bad service account is indistinguishable from an outage
        assert 'wazo-auth refused the service credentials' in caplog.text

    async def test_starting_twice_is_refused(self):
        async with self.renewer:
            with pytest.raises(RuntimeError):
                await self.renewer.start()
