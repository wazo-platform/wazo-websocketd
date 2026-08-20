# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from collections.abc import Callable

from wazo_auth_client.types import TokenDict

MasterTenantMatcher = Callable[[str], bool]


class TokenUser:
    def __init__(self, token: TokenDict, is_master_tenant: MasterTenantMatcher):
        self._token = token
        self._is_master_tenant = is_master_tenant

    @property
    def acl(self) -> list[str]:
        return self._token['acl']

    def is_admin(self) -> bool:
        purpose = self._token['metadata'].get('purpose', None)
        is_tenant_admin = bool(self._token['metadata'].get('admin', False))
        return (
            self.is_master_tenant()
            or is_tenant_admin
            or purpose in ('external_api', 'internal')
        )

    def is_master_tenant(self) -> bool:
        return self._is_master_tenant(self.tenant_uuid)

    @property
    def session_uuid(self) -> str:
        return self._token['session_uuid']

    @property
    def tenant_uuid(self) -> str:
        return self._token['metadata']['tenant_uuid']

    @property
    def token_id(self) -> str:
        return self._token['token']

    @property
    def token_utc_expires_at(self) -> str:
        return self._token['utc_expires_at']

    @property
    def uuid(self) -> str:
        return self._token['metadata']['uuid']
