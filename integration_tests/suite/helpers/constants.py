# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import os.path
from os import environ, path
from uuid import UUID

ASSET_ROOT = path.join(os.path.dirname(__file__), '..', '..', 'assets')

WAZO_ORIGIN_UUID = 'the-predefined-wazo-uuid'
START_TIMEOUT = int(environ.get('INTEGRATION_TEST_TIMEOUT', '60'))

INVALID_TOKEN_ID = 'invalid-token'
UNAUTHORIZED_TOKEN_ID = 'invalid-acl-token'

TOKEN_UUID = UUID('00000000-0000-4000-a000-0123456789ab')
MASTER_USER_UUID = UUID('00000000-0000-4000-a000-00000000beef')
MASTER_TENANT_UUID = UUID('00000000-0000-4000-a000-000000000001')
USER1_UUID = UUID('00000000-0000-4000-a000-000000000101')
USER2_UUID = UUID('00000000-0000-4000-a000-000000000102')
TENANT1_UUID = UUID('00000000-0000-4000-a000-000000000002')
TENANT2_UUID = UUID('00000000-0000-4000-a000-000000000003')

CLOSE_CODE_NO_TOKEN_ID = 4001
CLOSE_CODE_AUTH_FAILED = 4002
CLOSE_CODE_AUTH_EXPIRED = 4003
CLOSE_CODE_PROTOCOL_ERROR = 4004
