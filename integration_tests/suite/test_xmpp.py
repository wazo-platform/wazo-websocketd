# Copyright 2017 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0+

from .test_api.base import IntegrationTest, run_with_loop
from .test_api.constants import (
    VALID_TOKEN_ID,
)


class TestNoMongooseIM(IntegrationTest):

    asset = 'no_mongooseim_server'

    @run_with_loop
    def test_no_mongooseim_server_closes_websocket(self):
        yield from self.websocketd_client.connect_and_wait_for_close(VALID_TOKEN_ID)
