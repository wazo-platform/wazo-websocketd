# Copyright 2016-2022 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import aioamqp
import asyncio
import json
import logging

from aioamqp.exceptions import AmqpClosedConnection, ChannelClosed
from collections import namedtuple
from itertools import chain, count, cycle, repeat
from secrets import token_hex
from xivo.auth_verifier import AccessCheck

from .auth import get_master_tenant
from .exception import (
    BusConnectionError,
    BusConnectionLostError,
    EventPermissionError,
    InvalidEvent,
    InvalidTokenError,
)

logger = logging.getLogger(__name__)

_Event = namedtuple('Event', 'name, headers, acl, payload, message')
_ExchangeParams = namedtuple('_ExchangeParams', 'name, type')


class _BusConnection:
    _id_counter = count(1)

    def __init__(self, url, *, loop=None):
        self._id = next(_BusConnection._id_counter)
        self._url = url
        self._loop = loop or asyncio.get_event_loop()
        self._closing = asyncio.Event()
        self._connected = asyncio.Event()
        self._transport = None
        self._protocol = None
        self._consumers = []
        self._task = None

    @property
    def is_closing(self):
        return self._closing.is_set()

    @property
    def is_connected(self):
        return self._connected.is_set()

    async def run(self):
        while True:
            await self.connect()
            self._connected.set()

            # Wait for the connection to terminate
            await self._protocol.wait_closed()
            self._transport.close()
            self._connected.clear()

            # Notify consumers of disconnection
            await self._notify_closed()

            # if terminated, exit
            if self.is_closing:
                logger.debug('[connection %d] connection to bus closed', self._id)
                return

            logger.info(
                '[connection %d] unexpectedly lost connection to bus, attempting to reconnect...',
                self._id,
            )

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
                logger.info('[connection %d] connected to bus', self._id)
                return

    async def disconnect(self):
        self._closing.set()
        await self._protocol.close()
        self._transport.close()

    async def get_channel(self, *, wait=True):
        if not self.is_connected and not wait:
            raise BusConnectionError(
                f'[connection {self._id}] connection isn\'t established yet'
            )
        await self._connected.wait()
        try:
            return await self._protocol.channel()
        except (AmqpClosedConnection, ChannelClosed):
            raise BusConnectionError(
                f'[connection {self._id}] failed to create a new channel'
            )

    def spawn_consumer(self, exchange_params, token):
        consumer = BusConsumer(self, exchange_params, token)
        self._consumers.append(consumer)
        return consumer

    def remove_consumer(self, consumer):
        if consumer not in self._consumers:
            raise ValueError('consumer does not belong to this connection')
        self._consumers.remove(consumer)

    async def _notify_closed(self):
        tasks = [consumer.connection_lost() for consumer in self._consumers]
        await asyncio.gather(*tasks, loop=self._loop)


class _BusConnectionPool:
    def __init__(self, url, pool_size, *, loop=None):
        self._url = url
        self._loop = loop or asyncio.get_event_loop()
        self._connections = [_BusConnection(url, loop=loop) for _ in range(pool_size)]
        self._tasks = set()
        self._iterator = cycle(self._connections)

    @property
    def size(self):
        return len(self._connections)

    async def start(self):
        self._tasks = {
            self._loop.create_task(connection.run()) for connection in self._connections
        }
        logger.info('bus connection pool initialized with %d connections', self.size)

    async def stop(self):
        await asyncio.gather(
            *{connection.disconnect() for connection in self._connections}
        )

        # wait for connections to close gracefully or force after 5 sec
        _, pending = await asyncio.wait(self._tasks, loop=self._loop, timeout=5.0)
        if pending:
            logger.info('some connections did not exit gracefully, forcing...')
            for task in pending:
                task.cancel()

        logger.info('bus connection pool closed (%s connections)', self.size)

    def get_connection(self):
        return next(self._iterator)


class BusService:
    _DEFAULT_CONNECTION_POOL_SIZE = 2  # number of worker connections

    def __init__(self, config, *, loop=None):
        pool_size = config.get('pool_size', self._DEFAULT_CONNECTION_POOL_SIZE)

        self._url = 'amqp://{username}:{password}@{host}:{port}//'.format(**config)
        self._loop = loop or asyncio.get_event_loop()
        self._connection_pool = _BusConnectionPool(self._url, pool_size)
        self._exchange_params = _ExchangeParams(
            config['exchange_name'],
            config['exchange_type'],
        )

    def __enter__(self):
        self._loop.run_until_complete(self._connection_pool.start())

    def __exit__(self, *args):
        self._loop.run_until_complete(self._connection_pool.stop())

    async def create_consumer(self, token):
        connection = self._connection_pool.get_connection()
        return connection.spawn_consumer(self._exchange_params, token)

    def schedule_initialization(self):
        async def create_exchange():
            connection = self._connection_pool.get_connection()
            channel = await connection.get_channel(wait=True)
            await channel.exchange(
                self._exchange_params.name, self._exchange_params.type, durable=True
            )
            logger.debug('exchange `%s` initialized', self._exchange_params.name)
            await channel.close()

        # Migration <22.13
        # Upgrading from a previous version will keep `wazo-websocketd` exchange
        # since it is durable, but is no longer needed, so let's delete it if unused
        async def remove_deprecated():
            if self._exchange_params.name != 'wazo-websocketd':
                connection = self._connection_pool.get_connection()
                channel = await connection.get_channel(wait=True)

                await channel.exchange_delete('wazo-websocketd', if_unused=True)
                logger.debug('migration: removed legacy `wazo-websocketd` exchange...')
                await channel.close()

        self._loop.create_task(create_exchange())
        self._loop.create_task(remove_deprecated())


class BusConsumer:
    def __init__(self, connection, exchange_params, token):
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
        channel = self._channel = await self._connection.get_channel(wait=False)
        exchange = upstream = self._exchange_params.name

        # if not part of master tenant, create (if needed) tenant exchange
        if not self._is_master_tenant:
            exchange = self._generate_name(f'tenant-{self._tenant_uuid}')
            await channel.exchange(exchange, 'headers', durable=False, auto_delete=True)
            await channel.exchange_bind(
                exchange, upstream, '', arguments={'tenant_uuid': self._tenant_uuid}
            )
        self._exchange = exchange

        # Set QoS for messages
        await channel.basic_qos(prefetch_count=1, prefetch_size=0)

        # Create exclusive queue on exchange
        queue_name = self._generate_name(f'user-{self._uuid}', token_hex(3))
        response = await channel.queue(
            queue_name=queue_name, durable=False, auto_delete=True, exclusive=True
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
        logger.debug(
            f'''user `{self._uuid}` consuming {"all" if self._is_master_tenant else "tenant's" if self._is_admin else "user's"} events'''
        )

    async def _stop_consuming(self):
        if self._channel.is_open:
            if self._consumer_tag is not None:
                await self._channel.basic_cancel(self._consumer_tag)
            await self._channel.close()
        self._connection.remove_consumer(self)

    async def bind(self, event_name):
        bindings = [{}]
        if not self._is_admin:
            bindings = [{f'user_uuid:{uuid}': True} for uuid in (self._uuid, '*')]

        for binding in bindings:
            if event_name != '*':
                binding['name'] = event_name
            await self._channel.queue_bind(
                self._amqp_queue, self._exchange, '', arguments=binding
            )

    async def unbind(self, event_name):
        bindings = [{}]
        if not self._is_admin:
            bindings = [{f'user_uuid:{uuid}': True} for uuid in (self._uuid, '*')]

        for binding in bindings or [{}]:
            if event_name != '*':
                binding['name'] = event_name
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
    def _is_admin(self):
        try:
            purpose = self._token['metadata']['purpose']
            return self._is_master_tenant or purpose in ('internal', 'external_api')
        except KeyError:
            return False

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
    def _is_master_tenant(self):
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
        return '.'.join(['wazo-websocketd', *parts])
