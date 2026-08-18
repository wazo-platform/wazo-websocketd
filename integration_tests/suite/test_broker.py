# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from wazo_test_helpers import until

from .helpers.base import IntegrationTest, use_asset


@use_asset('base')
class TestBroker(IntegrationTest):
    async def test_validation_is_shared_across_workers(self):
        token = self.auth_client.make_token()
        self.auth_client.clear_recorded_requests()

        for _ in range(6):
            await self.websocketd_client.connect_and_wait_for_init(token)
            await self.websocketd_client.close()

        assert len(self._validations_of(token)) == 1
        self.auth_client.revoke_token(token)

    async def test_validation_falls_back_to_direct_auth_when_broker_dies(self):
        broker_pid = self.asset_cls.read_children_statuses()['broker']['pid']
        self.asset_cls.docker_exec(['sh', '-c', f'kill {broker_pid}'])
        self.addCleanup(self._restore_broker)

        with self.auth_client.token() as token:
            self.auth_client.clear_recorded_requests()
            await self.websocketd_client.connect_and_wait_for_init(token)
            assert len(self._validations_of(token)) == 1

    async def test_session_deleted_event_refuses_the_cached_token(self):
        session_uuid = 'aaaaaaaa-1111-4444-8888-cccccccccccc'
        token = self.auth_client.make_token(session_uuid=session_uuid)
        await self.websocketd_client.connect_and_wait_for_init(token)
        await self.websocketd_client.close()

        cached_before = self._broker_cache_size()
        await self.bus_client.publish_session_deleted(session_uuid)
        until.true(self._broker_evicted_one, cached_before, tries=20, interval=0.5)

        # the token is dead: re-entry is refused without asking wazo-auth again
        self.auth_client.clear_recorded_requests()
        await self.websocketd_client.connect_and_wait_for_close(token)
        assert self._validations_of(token) == []
        self.auth_client.revoke_token(token)

    def _broker_cache_size(self):
        # other sessions cache their own tokens, so only a decrease is meaningful
        return self.asset_cls.read_children_statuses()['broker']['cached_tokens']

    def _broker_evicted_one(self, cached_before):
        return self._broker_cache_size() < cached_before

    def _validations_of(self, token):
        return [
            request
            for request in self.auth_client.recorded_requests()
            if request['method'] == 'GET' and request['path'] == f'/0.1/token/{token}'
        ]

    def _restore_broker(self):
        self.asset_cls.restart_service('websocketd')
        self.asset_cls.wait_strategy.wait(self.asset_cls)
