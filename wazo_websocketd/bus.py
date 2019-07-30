# Copyright 2016-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import collections
import logging
import json

import asynqp

from .acl import ACLCheck
from .exception import (
    BusConnectionError,
    BusConnectionLostError,
)

logger = logging.getLogger(__name__)


def new_bus_event_service(config, loop):
    bus_connection = _BusConnection(config, loop)
    bus_event_dispatcher = _BusEventDispatcher()
    return _BusEventService(loop, bus_connection, bus_event_dispatcher)


class _BusConnection(object):

    def __init__(self, config, loop):
        self._host = config['bus']['host']
        self._port = config['bus']['port']
        self._username = config['bus']['username']
        self._password = config['bus']['password']
        self._exchange_name = config['bus']['exchange_name']
        self._exchange_type = config['bus']['exchange_type']
        self._loop = loop
        self._msg_received_callback = None
        self._connected = False
        self._closed = False

    @asyncio.coroutine
    def close(self):
        self._closed = True
        if self._connected:
            logger.debug('closing bus connection')
            self._connected = False
            try:
                yield from self._consumer.cancel()
                yield from self._channel.close()
                yield from self._connection.close()
            except Exception:
                logger.exception('unexpected error while closing bus connection')
        self._msg_received_callback = None

    def set_msg_received_callback(self, callback):
        # Must be called before calling "connect()". Can't be changed once connected.
        self._msg_received_callback = callback

    @property
    def connected(self):
        return self._connected

    @asyncio.coroutine
    def connect(self):
        if self._closed:
            raise Exception('already closed')
        if self._connected:
            raise Exception('already connected')

        logger.debug('connecting to bus')
        self._connected = True
        try:
            self._connection = yield from asynqp.connect(self._host, self._port, self._username, self._password, loop=self._loop)
            self._channel = yield from self._connection.open_channel()
            self._exchange = yield from self._channel.declare_exchange(self._exchange_name, self._exchange_type, durable=True)
            self._queue = yield from self._channel.declare_queue(exclusive=True)
            self._consumer = yield from self._queue.consume(self._msg_received_callback, no_ack=True)
        except Exception:
            logger.exception('error while connecting to the bus')
            self._connected = False
            raise BusConnectionError('error while connecting')
        # check if the connection was not lost while we were doing the other
        # initialization step (but the exception was not raised)
        if not self._connected:
            raise BusConnectionLostError()

    @asyncio.coroutine
    def add_queue_binding(self, routing_key):
        if not self._connected:
            raise BusConnectionError('not connected')
        yield from self._queue.bind(self._exchange, routing_key)

    def on_connection_lost(self):
        self._connected = False


class _BusEventService(object):

    def __init__(self, loop, bus_connection, bus_event_dispatcher):
        # Becomes the owner of the bus_connection
        self._loop = loop
        self._bus_connection = bus_connection
        self._bus_connection.set_msg_received_callback(self._on_msg_received)
        self._bus_event_dispatcher = bus_event_dispatcher
        self._lock = asyncio.Lock(loop=loop)

    def on_connection_lost(self):
        # Must only be called by the loop exception handler when a ConnectionLostError is raised
        self._bus_connection.on_connection_lost()
        self._bus_event_dispatcher.dispatch_connection_lost()

    def _on_msg_received(self, bus_msg):
        logger.debug('bus message received')
        try:
            bus_event = _decode_bus_msg(bus_msg)
        except ValueError as e:
            logger.debug('ignoring bus message: not a bus event: %s', e)
        else:
            self._bus_event_dispatcher.dispatch_event(bus_event)

    @asyncio.coroutine
    def close(self):
        yield from self._bus_connection.close()

    @asyncio.coroutine
    def new_event_consumer(self, token):
        # Try to establish a connection to the bus if not already established
        # first. Might raise an exception if connection fails.
        # Can be called by multiple coroutine at the same time.
        with (yield from self._lock):
            if not self._bus_connection.connected:
                yield from self._bus_connection.connect()
                # TODO: each connection should add new bindings according to the subscription type
                # (user or admin). An empty routing-key with headers exchange mean all events
                yield from self._bus_connection.add_queue_binding('')

        acl_check = ACLCheck(token['metadata']['uuid'], token['acls'])
        tenant_uuid = token['metadata']['tenant_uuid']
        bus_event_consumer = _BusEventConsumer(
            self._loop,
            self._bus_event_dispatcher,
            acl_check,
            tenant_uuid,
        )
        self._bus_event_dispatcher.add_event_consumer(bus_event_consumer)
        return bus_event_consumer


class _BusEventDispatcher(object):

    # XXX this class doesn't do much right now, it will probably be more useful
    #     when we'll need to optimize the dispatch process (i.e. move some logic
    #     from the consumer into the dispatcher for optimization purpose)

    def __init__(self):
        self._bus_event_consumers = set()

    def add_event_consumer(self, bus_event_consumer):
        self._bus_event_consumers.add(bus_event_consumer)

    def remove_event_consumer(self, bus_event_consumer):
        self._bus_event_consumers.discard(bus_event_consumer)

    def dispatch_connection_lost(self):
        for bus_event_consumer in self._bus_event_consumers:
            bus_event_consumer._on_connection_lost()

    def dispatch_event(self, bus_event):
        if bus_event.has_acl:
            logger.debug('dispatching event "%s" with ACL "%s"', bus_event.name, bus_event.acl)
            for bus_event_consumer in self._bus_event_consumers:
                bus_event_consumer._on_event(bus_event)
        else:
            logger.debug('not dispatching event "%s": event has no ACL', bus_event.name)


class _BusEventConsumer(object):

    def __init__(self, loop, bus_event_dispatcher, acl_check, tenant_uuid):
        self._bus_event_dispatcher = bus_event_dispatcher
        self._acl_check = acl_check
        self._queue = asyncio.Queue(loop=loop)
        self._events = set()
        self._all_events = False
        self._tenant_uuid = tenant_uuid

    def close(self):
        self._bus_event_dispatcher.remove_event_consumer(self)

    def _subscribe_to_event(self, event_name, tenant_uuid=None):
        if event_name == '*':
            self._all_events = True
        else:
            self._events.add((event_name, tenant_uuid))

    def subscribe_to_admin_event(self, event_name, tenant_uuid=None):
        subscribe_tenant_uuid = tenant_uuid or self._tenant_uuid
        self._subscribe_to_event(event_name, subscribe_tenant_uuid)
        # Since not every message has a tenant_uuid, admin need to be bound to all tenants
        self._subscribe_to_event(event_name, None)

    def subscribe_to_user_event(self, event_name):
        self._subscribe_to_event(event_name, self._tenant_uuid)

    @asyncio.coroutine
    def get(self):
        # Raise a BusConnectionLostError when the connection to the bus is lost.
        bus_event = yield from self._queue.get()
        if bus_event is None:
            raise BusConnectionLostError()
        return bus_event

    def _on_connection_lost(self):
        self._queue.put_nowait(None)

    def _on_event(self, bus_event):
        if self._all_events or (bus_event.name, bus_event.tenant_uuid) in self._events:
            if self._acl_check.matches_required_acl(bus_event.acl):
                self._queue.put_nowait(bus_event)


def _decode_bus_msg(bus_msg):
    msg_body = bus_msg.body.decode('utf-8')
    if not isinstance(json.loads(msg_body), dict):
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

    tenant_uuid = headers.get('tenant_uuid', None)

    return _BusEvent(name, has_acl, acl, tenant_uuid, msg_body)


_BusEvent = collections.namedtuple('_BusEvent', ['name', 'has_acl', 'acl', 'tenant_uuid', 'msg_body'])
