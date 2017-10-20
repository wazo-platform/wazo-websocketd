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

    @run_with_loop
    def test_presence_when_no_user_connected(self):
        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)
        msg = yield from self.websocketd_client.op_presence('random-uuid', 'dnd')
        self.assertEqual(msg['code'], 404)

    @run_with_loop
    def test_presence(self):
        yield from self.auth_server.put_token('other-token-id', xivo_user_uuid='other-xivo-user-uuid')
        other_client = self.new_websocketd_client()
        yield from other_client.connect_and_wait_for_init('other-token-id')

        yield from self.websocketd_client.connect_and_wait_for_init(self.valid_token_id)
        msg = yield from self.websocketd_client.op_presence('other-xivo-user-uuid', 'dnd')
        self.assertEqual(msg['code'], 0)
        yield from other_client.close()

        # FIXME: check that the presence really changed, when get_presence is implemented
