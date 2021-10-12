# Copyright 2016-2021 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from requests.packages.urllib3 import disable_warnings
from xivo_test_helpers.auth import AuthClient, MockUserToken


class AuthServer(object):
    def __init__(self, port):
        self._client = AuthClient('127.0.0.1', port)
        disable_warnings()

    def put_token(
        self,
        token_id,
        user_uuid='123-456',
        session_uuid='my-session',
        acl=['websocketd'],
    ):
        token = MockUserToken(token_id, user_uuid, session_uuid=session_uuid, acl=acl)
        self._client.set_token(token)

    def remove_token(self, token_id):
        self._client.revoke_token(token_id)
