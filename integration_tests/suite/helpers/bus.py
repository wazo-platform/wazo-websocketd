# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import json
import aioamqp
import asyncio

from aioamqp.exceptions import AmqpClosedConnection
from uuid import UUID

from .constants import START_TIMEOUT, TENANT1_UUID, USER1_UUID, WAZO_ORIGIN_UUID


class BusClient:
    timeout = START_TIMEOUT

    def __init__(self, port):
        self._port = port
        self._transport: asyncio.Transport = None  # type: ignore[assignment]
        self._protocol: aioamqp.protocol.AmqpProtocol = None  # type: ignore[assignment]
        self._channel: aioamqp.channel.Channel = None  # type: ignore[assignment]

    async def connect(self):
        await self._try_connect(timeout=self.timeout)
        self._channel = await self._protocol.channel()

    async def _try_connect(self, timeout):
        for _ in range(timeout):
            try:
                self._transport, self._protocol = await aioamqp.connect(
                    '127.0.0.1', self._port, login_method='PLAIN'
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

    async def publish(
        self,
        event: dict,
        tenant_uuid: str | UUID = TENANT1_UUID,
        user_uuid: str | UUID = USER1_UUID,
        *,
        origin_uuid: str | None = None,
    ) -> None:
        payload = json.dumps(event).encode('utf-8')
        exchange = 'wazo-headers'
        properties = {
            'headers': {
                'name': event['name'],
                'origin_uuid': origin_uuid or WAZO_ORIGIN_UUID,
            },
        }

        if tenant_uuid:
            properties['headers'].update(tenant_uuid=str(tenant_uuid))

        if user_uuid:
            properties['headers'].update({f'user_uuid:{user_uuid}': True})

        if 'required_acl' in event:
            properties['headers'].update(required_acl=event['required_acl'])

        await self._channel.publish(payload, exchange, '', properties=properties)
