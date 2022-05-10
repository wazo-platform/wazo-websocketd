# Copyright 2016-2022 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import json
import aioamqp
import asyncio
from aioamqp.exceptions import AmqpClosedConnection


class BusClient(object):
    timeout = int(os.environ.get('INTEGRATION_TEST_TIMEOUT', '30'))

    def __init__(self, port):
        self._port = port
        self._transport = None
        self._protocol = None
        self._channel = None

    async def connect(self):
        await self._try_connect(timeout=self.timeout)
        self._channel = await self._protocol.channel()

    async def _try_connect(self, timeout):
        for _ in range(timeout):
            try:
                self._transport, self._protocol = await aioamqp.connect(
                    '127.0.0.1', self._port
                )
            except (AmqpClosedConnection, OSError):
                await asyncio.sleep(1)
            else:
                return
        raise AmqpClosedConnection

    async def close(self):
        if self._channel and self._channel.is_open:
            await self._channel.close()
            await self._protocol.close()
            self._transport.close()

    async def publish(self, event, tenant_uuid=None):
        payload = json.dumps(event).encode('utf-8')
        exchange = 'wazo-websocketd'
        properties = {
            'headers': {
                'name': event['name'],
            },
        }

        if tenant_uuid:
            properties['headers']['tenant_uuid'] = str(
                event.get('tenant_uuid', tenant_uuid)
            )

        if 'required_acl' in event:
            properties['headers']['required_acl'] = event['required_acl']

        await self._channel.publish(payload, exchange, '', properties=properties)
