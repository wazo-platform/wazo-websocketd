# Copyright 2016-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import json

import asynqp


class BusClient(object):

    def __init__(self, loop, port):
        self._loop = loop
        self._port = port
        self._connection = None
        self._exchange = None

    @asyncio.coroutine
    def connect(self):
        self._connection = yield from asynqp.connect('localhost', loop=self._loop, port=self._port)
        self._channel = yield from self._connection.open_channel()
        self._exchange = yield from self._channel.declare_exchange('wazo-headers', 'headers', durable=True)

    @asyncio.coroutine
    def close(self):
        if self._connection:
            yield from self._channel.close()
            yield from self._connection.close()
            self._channel = None
            self._connection = None

    def publish_event(self, event):
        headers = {
            'name': event['name'],
            'required_acl': event.get('required_acl', event['name']),
            'tenant_uuid': event.get('tenant_uuid', None),
        }
        routing_key = ''
        self._exchange.publish(
            asynqp.Message(json.dumps(event), headers=headers),
            routing_key,
            mandatory=False,
        )
