# Copyright 2017 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0+

from contextlib import contextmanager

from .test_api.base import IntegrationTest, run_with_loop


class TestNoMongooseIM(IntegrationTest):

    asset = 'no_mongooseim_server'

    @run_with_loop
    def test_no_mongooseim_server_closes_websocket(self):
        yield from self.websocketd_client.connect_and_wait_for_close(self.valid_token_id)


class TestXMPPConnection(IntegrationTest):

    asset = 'basic'

    @run_with_loop
    def test_mongooseim_stop_after_connected(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)
        with self.mongooseim_stopped():
            yield from self.websocketd_client.wait_for_close()

    @contextmanager
    def mongooseim_stopped(self):
        self.stop_service('mongooseim', timeout=30)
        yield
        self.start_service('mongooseim')

    @run_with_loop
    def test_client_disconnect(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)
        yield from self.websocketd_client.close()
        sessions = self.mongooseim_client.sessions()
        if sessions:
            raise AssertionError('xmpp server contains openned sessions: {}'.format(sessions))

    @run_with_loop
    def test_no_remained_xmpp_session_when_websocketd_stopped(self):
        yield from self.auth_server.put_token('my-token-id', xivo_user_uuid='my-user-uuid')
        yield from self.websocketd_client.connect_and_wait_for_init('my-token-id')
        self.assertEqual(len(self.mongooseim_client.sessions()), 1)

        self.stop_service('websocketd')
        self.assertEqual(len(self.mongooseim_client.sessions()), 0)

        yield from self.websocketd_client.close()
        self.start_service('websocketd')


class TestWebsocketOperation(IntegrationTest):

    asset = 'basic'

    @run_with_loop
    def test_presence_when_no_acl_for_presence(self):
        token = 'only-valid-for-connection'
        yield from self.auth_server.put_token(token, acls=[])
        yield from self.websocketd_client.connect_and_wait_for_init(token)
        yield from self.auth_server.remove_token(token)
        msg = yield from self.websocketd_client.op_presence('123-456', 'dnd')
        self.assertEqual(msg['code'], 401)
