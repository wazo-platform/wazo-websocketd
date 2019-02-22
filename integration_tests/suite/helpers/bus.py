# Copyright 2016 Avencall
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
        self._exchange = yield from self._channel.declare_exchange('xivo', 'topic', durable=True)

    @asyncio.coroutine
    def close(self):
        if self._connection:
            yield from self._channel.close()
            yield from self._connection.close()
            self._channel = None
            self._connection = None

    def publish_event(self, event, routing_key=None):
        if routing_key is None:
            routing_key = event['name']
        self._exchange.publish(asynqp.Message(json.dumps(event)), routing_key, mandatory=False)
