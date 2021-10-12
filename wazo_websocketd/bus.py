# Copyright 2016-2021 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import collections
import logging
import json
import kombu

import asynqp

from .exception import BusConnectionError, BusConnectionLostError

logger = logging.getLogger(__name__)


ROUTING_KEYS = [
    'applications.#',
    'auth.#',
    'call_log.#',
    'call_logd.#',
    'calls.#',
    'chatd.#',
    'collectd.#',
    'conferences.#',
    'config.#',
    'directory.#',
    'faxes.#',
    'lines.#',
    'meetings.#',
    'plugin.#',
    'service.#',
    'status.#',
    'switchboards.#',
    'sysconfd.#',
    'trunks.#',
    'voicemails.#',
]


def create_or_update_exchange(config):
    bus_url = 'amqp://{username}:{password}@{host}:{port}//'.format(**config['bus'])

    upstream_exchange = kombu.Exchange(
        config['bus']['upstream_exchange_name'],
        type=config['bus']['upstream_exchange_type'],
        auto_delete=False,
        durable=True,
        delivery_mode='persistent',
    )
    exchange = kombu.Exchange(
        config['bus']['exchange_name'],
        type=config['bus']['exchange_type'],
        auto_delete=False,
        durable=True,
        delivery_mode='persistent',
    )

    with kombu.Connection(bus_url) as connection:
        upstream_exchange.bind(connection).declare()
        exchange = exchange.bind(connection)
        exchange.declare()
        # This unbind_from and the one in the loop were added in 20.01 because we created
        # a bind on the wrong exchange (wazo-headers) in a previous version
        exchange.unbind_from('wazo-headers', 'trunks.#voicemails.#')  # Migrate <20.01
        for routing_key in ROUTING_KEYS:
            exchange.unbind_from(
                'wazo-headers', routing_key=routing_key
            )  # Migrate <20.01
            exchange.bind_to(upstream_exchange, routing_key=routing_key)


def new_bus_event_service(config):
    bus_connection = _BusConnection(config)
    return _BusEventService(bus_connection)


class _BusConnection(object):
    def __init__(self, config):
        self._host = config['bus']['host']
        self._port = config['bus']['port']
        self._username = config['bus']['username']
        self._password = config['bus']['password']
        self._exchange_name = config['bus']['exchange_name']
        self._exchange_type = config['bus']['exchange_type']
        self._msg_received_callback = None
        self._connection_lost_callback = None
        self._connected = False
        self._closed = False

    async def close(self):
        self._closed = True
        if self._connected:
            logger.debug('closing bus connection')
            self._connected = False
            try:
                await self._consumer.cancel()
                await self._channel.close()
                await self._connection.close()
            except Exception:
                logger.exception('unexpected error while closing bus connection')
        self._msg_received_callback = None

    def set_msg_received_callback(self, callback):
        # Must be called before calling "connect()". Can't be changed once connected.
        self._msg_received_callback = callback

    def set_connection_lost_callback(self, callback):
        self._connection_lost_callback = callback

    @property
    def connected(self):
        return self._connected

    async def connect(self):
        if self._closed:
            raise Exception('already closed')
        if self._connected:
            raise Exception('already connected')

        logger.debug('connecting to bus')
        self._connected = True
        try:
            self._connection = await asynqp.connect(
                self._host,
                self._port,
                self._username,
                self._password,
                on_connection_close=self.on_connection_closed,
            )
            self._channel = await self._connection.open_channel()
            self._exchange = await self._channel.declare_exchange(
                self._exchange_name, self._exchange_type, durable=True
            )
            self._queue = await self._channel.declare_queue(exclusive=True)
            self._consumer = await self._queue.consume(
                self._msg_received_callback, no_ack=True
            )
        except Exception:
            logger.exception('error while connecting to the bus')
            self._connected = False
            raise BusConnectionError('error while connecting')
        # check if the connection was not lost while we were doing the other
        # initialization step (but the exception was not raised)
        if not self._connected:
            raise BusConnectionLostError()

    async def add_queue_binding(self, routing_key):
        if not self._connected:
            raise BusConnectionError('not connected')
        await self._queue.bind(self._exchange, routing_key)

    async def on_connection_closed(self, exception):
        logger.debug('Connection closed: %s', exception)
        if self._connected:
            self._connected = False
            self._connection_lost_callback(exception)


class _BusEventService(object):
    def __init__(self, bus_connection):
        # Becomes the owner of the bus_connection
        self._bus_connection = bus_connection
        self._bus_connection.set_msg_received_callback(self._on_msg_received)
        self._bus_connection.set_connection_lost_callback(
            lambda exc: self.on_connection_lost()
        )
        self._lock = asyncio.Lock()
        self._bus_event_consumers = set()

    def on_connection_lost(self):
        for bus_event_consumer in self._bus_event_consumers:
            bus_event_consumer.put_connection_lost()

    def _on_msg_received(self, bus_msg):
        logger.debug('bus message received')
        try:
            bus_event = _decode_bus_msg(bus_msg)
        except ValueError as e:
            logger.debug('ignoring bus message: not a bus event: %s', e)
        else:
            if bus_event.has_acl:
                logger.debug(
                    'dispatching event "%s" with ACL "%s"',
                    bus_event.name,
                    bus_event.acl,
                )
                for bus_event_consumer in self._bus_event_consumers:
                    bus_event_consumer.put(bus_event)
            else:
                logger.debug(
                    'not dispatching event "%s": event has no ACL', bus_event.name
                )

    async def close(self):
        await self._bus_connection.close()

    async def register_event_consumer(self, bus_event_consumer):
        # Try to establish a connection to the bus if not already established
        # first. Might raise an exception if connection fails.
        # Can be called by multiple coroutine at the same time.
        with (await self._lock):
            if not self._bus_connection.connected:
                await self._bus_connection.connect()
                # TODO: each connection should add new bindings according to the subscription type
                # (user or admin). An empty routing-key with headers exchange mean all events
                await self._bus_connection.add_queue_binding('')

        self._bus_event_consumers.add(bus_event_consumer)

    def unregister_event_consumer(self, bus_event_consumer):
        self._bus_event_consumers.discard(bus_event_consumer)


def _decode_bus_msg(bus_msg):
    msg_body = bus_msg.body.decode('utf-8')
    body = json.loads(msg_body)
    if not isinstance(body, dict):
        raise ValueError('not a valid json document')

    headers = bus_msg.headers

    if 'name' not in headers:
        raise ValueError('object is missing required "name" key')
    name = headers['name']
    if not isinstance(name, str):
        raise ValueError('object "name" value is not a string')

    if 'required_acl' in headers:
        has_acl = True
        acl = headers['required_acl']
        if acl is not None and not isinstance(acl, str):
            raise ValueError('object "required_acl" value is not a string nor null')
    else:
        has_acl = False
        acl = None

    return _BusEvent(name, has_acl, acl, msg_body, body)


_BusEvent = collections.namedtuple(
    '_BusEvent', ['name', 'has_acl', 'acl', 'msg_body', 'body']
)
