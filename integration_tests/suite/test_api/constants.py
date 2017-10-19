# Copyright 2016-2017 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0+

import os.path

ASSET_ROOT = os.path.join(os.path.dirname(__file__), '..', '..', 'assets')

INVALID_TOKEN_ID = 'invalid-token'
UNAUTHORIZED_TOKEN_ID = 'invalid-acl-token'

CLOSE_CODE_NO_TOKEN_ID = 4001
CLOSE_CODE_AUTH_FAILED = 4002
CLOSE_CODE_AUTH_EXPIRED = 4003
CLOSE_CODE_PROTOCOL_ERROR = 4004
