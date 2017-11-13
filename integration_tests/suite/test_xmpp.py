# Copyright 2017 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0+

import time

from contextlib import contextmanager

from .helpers.base import IntegrationTest, run_with_loop
from .helpers.constants import (
    VALID_USER_CONNECTED,
    VALID_USER_DISCONNECTED,
    VALID_USER_DISCONNECTING,
)


class TestNoMongooseIM(IntegrationTest):

    asset = 'no_mongooseim_server'

    @run_with_loop
    def test_no_mongooseim_server_closes_websocket(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)
        self.websocketd_client._started = True
        yield from self.websocketd_client.op_start()
        yield from self.websocketd_client.wait_for_close()

    @run_with_loop
    def test_set_presence_return_error(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id_without_user)
        msg = yield from self.websocketd_client.op_set_presence(VALID_USER_DISCONNECTING, 'dnd')
        self.assertEqual(msg['code'], 503)


class TestXMPPConnection(IntegrationTest):

    asset = 'basic'

    @run_with_loop
    def test_mongooseim_stop_after_connected(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)
        yield from self.websocketd_client.op_start()
        with self.mongooseim_stopped():
            yield from self.websocketd_client.wait_for_close()

    @contextmanager
    def mongooseim_stopped(self):
        self.stop_service('mongooseim', timeout=30)
        yield
        self.start_service('mongooseim')
        time.sleep(3)  # initiliaze MongooseIM

    @run_with_loop
    def test_client_disconnect(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)
        yield from self.websocketd_client.op_start()
        yield from self.websocketd_client.close()
        sessions = self.mongooseim_client.sessions()
        if sessions:
            raise AssertionError('xmpp server contains openned sessions: {}'.format(sessions))

    @run_with_loop
    def test_no_remained_xmpp_session_when_websocketd_stopped(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)
        yield from self.websocketd_client.op_start()
        self.assertEqual(len(self.mongooseim_client.sessions()), 1)

        self.stop_service('websocketd')
        self.assertEqual(len(self.mongooseim_client.sessions()), 0)

        yield from self.websocketd_client.close()
        self.start_service('websocketd')


class TestWebsocketOperation(IntegrationTest):

    asset = 'basic'

    @run_with_loop
    def test_get_presence_when_no_acl_for_presence(self):
        yield from self.auth_server.put_token('token-id')
        yield from self.websocketd_client.connect_and_wait_for_init('token-id')
        msg = yield from self.websocketd_client.op_get_presence(VALID_USER_CONNECTED)
        self.assertEqual(msg['code'], 401)

    @run_with_loop
    def test_get_presence_with_user_connected(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id_without_user)
        msg = yield from self.websocketd_client.op_get_presence(VALID_USER_CONNECTED)
        self.assertEqual(msg['msg']['presence'], 'available')

    @run_with_loop
    def test_get_presence_with_user_disconnected(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id_without_user)
        msg = yield from self.websocketd_client.op_get_presence(VALID_USER_DISCONNECTED)
        self.assertEqual(msg['msg']['presence'], 'disconnected')

    @run_with_loop
    def test_set_presence_when_no_acl_for_presence(self):
        yield from self.auth_server.put_token('token-id')
        yield from self.websocketd_client.connect_and_wait_for_init('token-id')
        msg = yield from self.websocketd_client.op_set_presence('123-456', 'dnd')
        self.assertEqual(msg['code'], 401)

    @run_with_loop
    def test_set_presence(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id_without_user)
        msg = yield from self.websocketd_client.op_set_presence(VALID_USER_CONNECTED, 'dnd')
        self.assertEqual(msg['code'], 0)

    @run_with_loop
    def test_set_presence_dynamically_connect_user(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id_without_user)
        yield from self.websocketd_client.op_set_presence(VALID_USER_DISCONNECTED, 'dnd')
        self.assertEqual(len(self.mongooseim_client.sessions()), 1)
