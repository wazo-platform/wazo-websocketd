# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio

import asynqp


class BusClient(object):

    def __init__(self, loop):
        self._loop = loop
        self._connection = None
        self._exchanges = {}

    @asyncio.coroutine
    def connect(self):
        self._connection = yield from asynqp.connect('localhost', loop=self._loop)
        self._channel = yield from self._connection.open_channel()

    @asyncio.coroutine
    def close(self):
        if self._connection:
            yield from self._channel.close()
            yield from self._connection.close()
            self._channel = None
            self._connection = None

    @asyncio.coroutine
    def declare_exchange(self, name, type_, durable):
        if name in self._exchanges:
            return
        exchange = yield from self._channel.declare_exchange(name, type_, durable=durable)
        self._exchanges[name] = exchange

    @asyncio.coroutine
    def declare_xivo_exchange(self):
        yield from self.declare_exchange('xivo', 'topic', True)

    def publish(self, exchange_name, routing_key, body):
        msg = asynqp.Message(body)
        exchange = self._exchanges[exchange_name]
        exchange.publish(msg, routing_key)

    def publish_on_xivo(self, routing_key, body):
        self.publish('xivo', routing_key, body)
