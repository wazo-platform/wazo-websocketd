# Copyright 2016-2022 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging
import json
import aioamqp

from aioamqp.exceptions import AmqpClosedConnection, ChannelClosed
from itertools import cycle, repeat, chain
from collections import namedtuple
from xivo.auth_verifier import AccessCheck
from .exception import (
    BusConnectionError,
    BusConnectionLostError,
    InvalidTokenError,
    InvalidEvent,
    EventPermissionError,
)
from .auth import get_master_tenant

logger = logging.getLogger(__name__)

_Event = namedtuple('Event', 'name, headers, acl, payload, message')
_ConnectionParams = namedtuple(
    '_ConnectionParams', 'host, port, username, password, vhost'
)
_ExchangeParams = namedtuple('_ExchangeParams', 'name, type')

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
    async def process(config, timeout=30):
        url = 'amqp://{username}:{password}@{host}:{port}//'.format(**config['bus'])
        upstream_name = config['bus']['upstream_exchange_name']
        exchange_name = config['bus']['exchange_name']

        logger.debug('waiting on RabbitMQ... (timeout in %d second(s))', timeout)
        for attempt in range(timeout):
            try:
                transport, protocol = await aioamqp.from_url(url, heartbeat=60)
                break
            except (AmqpClosedConnection, OSError):
                if attempt >= (timeout - 1):
                    raise BusConnectionError
                await asyncio.sleep(1)

        channel = await protocol.channel()

        await channel.exchange_declare(
            upstream_name,
            config['bus']['upstream_exchange_type'],
            durable=True,
            auto_delete=False,
        )

        await channel.exchange_declare(
            exchange_name,
            config['bus']['exchange_type'],
            durable=True,
            auto_delete=False,
        )

        # This unbind and the one in the loop were added in 20.01 because we created
        # a bind on the wrong exchange (wazo-headers) in a previous version
        await channel.exchange_unbind(
            exchange_name, 'wazo-headers', 'trunks.#voicemails.#'
        )
        for routing_key in ROUTING_KEYS:
            await channel.exchange_unbind(
                exchange_name, 'wazo-headers', routing_key
            )  # Migrate <20.01
            await channel.exchange_bind(exchange_name, upstream_name, routing_key)

        await channel.close()
        await protocol.close()
        transport.close()

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(process(config))
    except BusConnectionError:
        logger.error(
            'Timed out while trying to connect to RabbitMQ, skipping exchange initialization...'
        )
    else:
        logger.debug('done configuring RabbitMQ, continuing...')


class _BusConnection:
    def __init__(self, id, url, *, loop=None):
        self._id = id
        self._url = url
        self._loop = loop or asyncio.get_event_loop()
        self._closing = asyncio.Event()
        self._transport = None
        self._protocol = None
        self._consumers = []

    @property
    def is_closing(self):
        return self._closing.is_set()

    @property
    async def is_open(self):
        try:
            await self._protocol.ensure_open()
        except AmqpClosedConnection:
            return False
        else:
            return True

    async def connect(self):
        timeouts = chain((1, 2, 4, 8, 16), repeat(32))
        while True:
            try:
                transport, protocol = await aioamqp.from_url(self._url, heartbeat=10)
            except (AmqpClosedConnection, OSError):
                timeout = next(timeouts)
                logger.debug(
                    '[connection %d] unable to connect, retrying in %d seconds',
                    self._id,
                    timeout,
                )
                await asyncio.sleep(timeout)
            else:
                self._transport, self._protocol = transport, protocol
                self._loop.create_task(self._handle_reconnection())
                return

    async def disconnect(self):
        self._closing.set()
        await self._protocol.close()
        self._transport.close()

    async def register(self, consumer):
        self._consumers.append(consumer)
        try:
            return await self._protocol.channel()
        except (AmqpClosedConnection, ChannelClosed):
            raise BusConnectionError(
                '[connection %d] unable to create channel', self._id
            )

    def unregister(self, consumer):
        self._consumers.remove(consumer)

    async def _handle_reconnection(self):
        while True:
            # Wait for the connection to terminate
            await self._protocol.wait_closed()
            self._transport.close()

            # Notify consumers of disconnection
            await self._notify_closed()

            # if terminated, exit
            if self.is_closing:
                return

            logger.info(
                '[connection %d] unexpectedly lost connection to bus, reconnecting...',
                self._id,
            )
            await self.connect()
            logger.info('[connection %d] reestablished connection to bus', self._id)

    async def _notify_closed(self):
        tasks = [consumer.connection_lost() for consumer in self._consumers]
        await asyncio.gather(*tasks, loop=self._loop)


class _BusConnectionPool:
    def __init__(self, url, pool_size, *, loop=None):
        self._url = url
        self._loop = loop or asyncio.get_event_loop()
        self._size = pool_size
        self._connections = []
        self._iterator = None

    @property
    def _round_robin(self):
        for index in cycle(range(self._size)):
            yield self._connections[index]

    async def connect(self):
        self._connections = [
            _BusConnection(id + 1, self._url, loop=self._loop)
            for id in range(self._size)
        ]

        await asyncio.gather(
            *[connection.connect() for connection in self._connections]
        )
        logger.info('bus connection pool initialized with %d connections', self._size)

    async def disconnect(self):
        await asyncio.gather(
            *[connection.disconnect() for connection in self._connections]
        )
        logger.info('bus connection pool closed (%s connections)', self._size)

    def connection(self):
        if not self._iterator:
            self._iterator = self._round_robin
        return next(self._iterator)


class BusService:
    _DEFAULT_CONNECTION_POOL_SIZE = 2  # number of worker connections

    def __init__(self, config, *, loop=None):
        self._url = 'amqp://{username}:{password}@{host}:{port}//'.format(
            **config['bus']
        )
        self._loop = loop or asyncio.get_event_loop()
        self._connection_pool = _BusConnectionPool(
            self._url, self._DEFAULT_CONNECTION_POOL_SIZE
        )
        self._exchange_params = _ExchangeParams(
            config['bus']['exchange_name'],
            config['bus']['exchange_type'],
        )

    def __enter__(self):
        self._loop.run_until_complete(self._connection_pool.connect())

    def __exit__(self, *args):
        self._loop.run_until_complete(self._connection_pool.disconnect())

    async def spawn(self, token):
        connection = self._connection_pool.connection()
        consumer = BusConsumer(self._exchange_params, connection, token)
        return consumer


class BusConsumer:
    def __init__(self, exchange_params, connection, token):
        self.set_token(token)

        self._exchange_params = exchange_params
        self._queue = asyncio.Queue()
        self._connection = connection
        self._channel = None
        self._consumer_tag = None
        self._exchange = None
        self._amqp_queue = None

    async def __aenter__(self):
        await self._start_consuming()
        return self

    async def __aexit__(self, *args):
        await self._stop_consuming()

    def __aiter__(self):
        return self

    async def __anext__(self):
        payload = await self._queue.get()
        if isinstance(payload, Exception):
            raise payload
        return payload

    async def _start_consuming(self):
        channel = self._channel = await self._connection.register(self)
        exchange = upstream = self._exchange_params.name

        # if not part of master tenant, create (if needed) tenant exchange
        if self._tenant_uuid != get_master_tenant():
            exchange = self._generate_name('tenant', self._tenant_uuid)
            await channel.exchange(exchange, 'headers', durable=False, auto_delete=True)
            await channel.exchange_bind(
                exchange, upstream, '', arguments={'tenant_uuid': self._tenant_uuid}
            )
        self._exchange = exchange

        # Set QoS for messages
        await channel.basic_qos(prefetch_count=1, prefetch_size=0)

        # Create exclusive queue on exchange
        response = await channel.queue(
            '', durable=False, auto_delete=True, exclusive=True
        )
        if response['queue'] is None:
            raise BusConnectionError
        self._amqp_queue = response['queue']

        # Start consuming on queue
        response = await self._channel.basic_consume(
            self._on_message, queue_name=self._amqp_queue, exclusive=True
        )
        if response['consumer_tag'] is None:
            raise BusConnectionError
        self._consumer_tag = response['consumer_tag']

    async def _stop_consuming(self):
        if self._channel.is_open:
            if self._consumer_tag is not None:
                await self._channel.basic_cancel(self._consumer_tag)
            await self._channel.close()
        self._connection.unregister(self)

    async def bind(self, event_name):
        binding = {}
        if event_name != '*':
            binding['name'] = event_name

        # TODO: Uncomment when all events are tagged with user_uuid:{uuid} or user_uuid:*
        # if not self._is_admin:
        #    binding.update(
        #        {f'user_uuid:{self._uuid}': True, 'user_uuid:*': True, 'x-match': 'any'}
        #    )

        await self._channel.queue_bind(
            self._amqp_queue, self._exchange, '', arguments=binding
        )

    async def unbind(self, event_name):
        binding = {}
        if event_name != '*':
            binding['name'] = event_name

        # TODO: Uncomment when all events are tagged with user_uuid:{uuid} or user_uuid:*
        # if not self._is_admin:
        #    binding.update(
        #        {f'user_uuid:{self._uuid}': True, 'user_uuid:*': True, 'x-match': 'any'}
        #    )

        await self._channel.queue_unbind(
            self._amqp_queue, self._exchange, '', arguments=binding
        )

    async def connection_lost(self):
        await self._queue.put(BusConnectionLostError())

    def get_token(self):
        return self._token

    def set_token(self, token):
        self._access = None
        self._token = None
        try:
            uuid = token['metadata']['uuid']
            session = token['session_uuid']
            acl = token['acl']
        except KeyError:
            raise InvalidTokenError('Malformed token received, missing token details')
        else:
            self._token = token
            self._access = AccessCheck(uuid, session, acl)

    @property
    def _uuid(self):
        try:
            return self._token['metadata']['uuid']
        except KeyError:
            return None

    @property
    def _tenant_uuid(self):
        try:
            return self._token['metadata']['tenant_uuid']
        except KeyError:
            return None

    @property
    def _session_uuid(self):
        try:
            return self._token['session_uuid']
        except KeyError:
            return None

    @property
    def _is_admin(self):
        try:
            return self._token['metadata']['tenant_uuid'] == get_master_tenant()
        except KeyError:
            return False

    @property
    def _has_access(self):
        return self._access.matches_required_access

    async def _on_message(self, channel, body, envelope, properties):
        try:
            event = self._decode(body, properties)
        except InvalidEvent as exc:
            logger.error('error during message decoding (reason: %s)', exc)
        except EventPermissionError as exc:
            logger.debug('discarding event (reason: %s)', exc)
        else:
            await self._queue.put(event)
            logger.debug('dispatching %s', event)
        finally:
            await channel.basic_client_ack(envelope.delivery_tag)

    def _decode(self, body, properties):
        headers = properties.headers

        try:
            stringified = body.decode('utf-8')
        except UnicodeDecodeError:
            raise InvalidEvent('unable to decode message')

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise InvalidEvent('invalid JSON')
        if not isinstance(payload, dict):
            raise InvalidEvent('not a dictionary')

        name = headers.get('name') or payload.get('name')
        if not name:
            raise InvalidEvent('missing event \'name\' field')

        if 'required_acl' not in headers:
            raise EventPermissionError(f'event \'{name}\' contains no ACL')
        acl = headers.get('required_acl')

        if acl is not None and not isinstance(acl, str):
            raise InvalidEvent('ACL must be string, not {}'.format(type(acl)))
        if not self._has_access(acl):
            raise EventPermissionError(
                f'user \'{self._uuid}\' is missing required ACL \'{acl}\' for event \'{name}\''
            )

        return _Event(name, headers, acl, payload, stringified)

    @staticmethod
    def _generate_name(*parts):
        module = __name__.split('.')[0]
        return '.'.join([module, *parts])
