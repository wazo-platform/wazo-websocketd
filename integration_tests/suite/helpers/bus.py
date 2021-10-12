# Copyright 2016-2021 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import json

import asynqp


class BusClient(object):
    def __init__(self, port):
        self._port = port
        self._connection = None
        self._exchange = None

    async def connect(self):
        self._connection = await asynqp.connect('127.0.0.1', port=self._port)
        self._channel = await self._connection.open_channel()
        self._exchange = await self._channel.declare_exchange(
            'wazo-websocketd', 'headers', durable=True
        )

    async def close(self):
        if self._connection:
            await self._channel.close()
            await self._connection.close()
            self._channel = None
            self._connection = None

    def publish_event(self, event):
        headers = {
            'name': event['name'],
            'required_acl': event.get('required_acl', event['name']),
        }
        routing_key = ''
        self._exchange.publish(
            asynqp.Message(json.dumps(event), headers=headers),
            routing_key,
            mandatory=False,
        )
