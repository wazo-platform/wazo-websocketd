# Copyright 2017 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0+

from contextlib import contextmanager

from .test_api.base import IntegrationTest, run_with_loop
from .test_api.constants import (
    VALID_TOKEN_ID,
)


class TestNoMongooseIM(IntegrationTest):

    asset = 'no_mongooseim_server'

    @run_with_loop
    def test_no_mongooseim_server_closes_websocket(self):
        yield from self.websocketd_client.connect_and_wait_for_close(VALID_TOKEN_ID)


class TestXMPPConnection(IntegrationTest):

    asset = 'basic'

    @run_with_loop
    def test_mongooseim_stop_after_connected(self):
        yield from self.websocketd_client.connect_and_wait_for_init(VALID_TOKEN_ID)
        with self.mongooseim_stopped():
            yield from self.websocketd_client.wait_for_close()

    @contextmanager
    def mongooseim_stopped(self):
        self.stop_service('mongooseim', timeout=30)
        yield
        self.start_service('mongooseim')

    @run_with_loop
    def test_client_disconnect(self):
        yield from self.websocketd_client.connect_and_wait_for_init(VALID_TOKEN_ID)
        yield from self.websocketd_client.close()
        sessions = self.mongooseim_client.sessions()
        if sessions:
            raise AssertionError('xmpp server contains openned sessions: {}'.format(sessions))
