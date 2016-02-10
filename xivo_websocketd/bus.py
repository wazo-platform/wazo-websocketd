# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio

import asynqp


class BusServiceFactory(object):

    def __init__(self, config):
        self._config = config

    def new_bus_service(self):
        exchange_declarator = _ExchangeDeclarator(self._config['bus_exchanges'])
        return _BusService(self._config, exchange_declarator)


class _BusService(object):

    def __init__(self, config, exchange_declarator):
        self._host = config['bus_host']
        self._port = config['bus_port']
        self._username = config['bus_username']
        self._password = config['bus_password']
        self._exchange_declarator = exchange_declarator
        self._msg_callback = None

    def set_callback(self, msg_callback):
        # must be called *before* connect
        self._msg_callback = msg_callback

    @asyncio.coroutine
    def connect(self):
        self._connection = yield from asynqp.connect(self._host, self._port, self._username, self._password)
        self._channel = yield from self._connection.open_channel()
        self._queue = yield from self._channel.declare_queue(exclusive=True)
        self._consumer = yield from self._queue.consume(self._msg_callback, no_ack=True)

    @asyncio.coroutine
    def close(self):
        yield from self._consumer.cancel()
        yield from self._channel.close()
        yield from self._connection.close()

    @asyncio.coroutine
    def bind(self, exchange_name, routing_key):
        exchange = yield from self._exchange_declarator.declare(exchange_name, self._channel)
        if not exchange:
            return False

        yield from self._queue.bind(exchange, routing_key)
        return True


class _ExchangeDeclarator(object):

    def __init__(self, exchange_definitions):
        self._exchange_definitions = exchange_definitions
        self._declared_exchanges = {}

    @asyncio.coroutine
    def declare(self, exchange_name, channel):
        if exchange_name in self._declared_exchanges:
            return self._declared_exchanges[exchange_name]

        definition = self._exchange_definitions.get(exchange_name)
        if not definition:
            return None

        exchange = yield from channel.declare_exchange(exchange_name, definition['type'],
                                                       durable=definition['durable'])
        self._declared_exchanges[exchange_name] = exchange
        return exchange