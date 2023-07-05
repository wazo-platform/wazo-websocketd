# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from datetime import datetime, timedelta

from requests.packages.urllib3 import disable_warnings
from contextlib import contextmanager
from wazo_test_helpers.auth import AuthClient as Client, MockUserToken

from .constants import TENANT1_UUID, USER1_UUID


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
        user_uuid=USER1_UUID,
        tenant_uuid=TENANT1_UUID,
        session_uuid='my-session-uuid',
        acl=['websocketd'],
        purpose='user',
        admin=False,
        expiration=60,
    ):
        metadata = {'tenant_uuid': str(tenant_uuid), 'purpose': purpose}
        utc_expires_at = str(datetime.utcnow() + timedelta(seconds=expiration))
        if purpose == 'user':
            metadata.update(admin=admin)

        if token_uuid is None:
            token = MockUserToken.some_token(
                user_uuid=str(user_uuid),
                session_uuid=str(session_uuid),
                acl=acl,
                metadata=metadata,
                utc_expires_at=utc_expires_at,
            )
        else:
            token = MockUserToken(
                str(token_uuid),
                str(user_uuid),
                session_uuid=str(session_uuid),
                metadata=metadata,
                acl=acl,
                utc_expires_at=utc_expires_at,
            )
        self._client.set_token(token)
        return token.token_id

    @contextmanager
    def token(
        self,
        *,
        token_uuid=None,
        user_uuid=USER1_UUID,
        tenant_uuid=TENANT1_UUID,
        session_uuid='my-session-uuid',
        acl=['websocketd'],
        purpose='user',
        admin=False,
        expiration=60,
    ):
        token = self.make_token(
            token_uuid=token_uuid,
            user_uuid=user_uuid,
            tenant_uuid=tenant_uuid,
            session_uuid=session_uuid,
            acl=acl,
            purpose=purpose,
            admin=admin,
            expiration=expiration,
        )
        try:
            yield token
        finally:
            self.revoke_token(token)
