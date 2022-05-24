# Copyright 2016-2022 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from requests.packages.urllib3 import disable_warnings
from contextlib import contextmanager
from wazo_test_helpers.auth import AuthClient as Client, MockUserToken

from .constants import TENANT1_UUID


class AuthClient:
    def __init__(self, port):
        self._client = Client('127.0.0.1', port)
        disable_warnings()

    # Proxy unknown method to client
    def __getattr__(self, attr):
        return getattr(self._client, attr)

    def make_token(
        self,
        *,
        token_uuid=None,
        user_uuid=None,
        tenant_uuid=TENANT1_UUID,
        session_uuid='my-session-uuid',
        acl=['websocketd']
    ):
        metadata = {'tenant_uuid': str(tenant_uuid)}
        if token_uuid is None:
            token = MockUserToken.some_token(
                session_uuid=str(session_uuid), acl=acl, metadata=metadata
            )
        else:
            token = MockUserToken(
                str(token_uuid),
                str(user_uuid),
                session_uuid=str(session_uuid),
                metadata=metadata,
                acl=acl,
            )
        self._client.set_token(token)
        return token.token_id

    @contextmanager
    def token(
        self,
        *,
        token_uuid=None,
        user_uuid=None,
        tenant_uuid=TENANT1_UUID,
        session_uuid='my-session-uuid',
        acl=['websocketd']
    ):
        token = self.make_token(
            token_uuid=token_uuid,
            user_uuid=user_uuid,
            tenant_uuid=tenant_uuid,
            session_uuid=session_uuid,
            acl=acl,
        )
        try:
            yield token
        finally:
            self.revoke_token(token)
