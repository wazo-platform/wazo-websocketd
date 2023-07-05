# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import aioamqp
import asyncio
import json
import logging

from aioamqp import AmqpProtocol
from aioamqp.channel import Channel
from aioamqp.envelope import Envelope
from aioamqp.properties import Properties
from aioamqp.exceptions import AmqpClosedConnection, ChannelClosed
from itertools import chain, cycle, repeat
from multiprocessing import Value
from secrets import token_hex
from typing import NamedTuple
from xivo.auth_verifier import AccessCheck

from .auth import MasterTenantProxy
from .exception import (
    BusConnectionError,
    BusConnectionLostError,
    EventPermissionError,
    InvalidEvent,
    InvalidTokenError,
)

logger = logging.getLogger(__name__)


class _UserHelper:
    def __init__(self, token: dict):
        self._token = token

    @property
    def acl(self) -> list[str]:
        return self._token['acl']

    def is_admin(self) -> bool:
        purpose = self._token['metadata'].get('purpose', None)
        is_tenant_admin = self._token['metadata'].get('admin', False)
        return (
            self.is_master_tenant()
            or is_tenant_admin
            or purpose in ('external_api', 'internal')
        )

    def is_master_tenant(self) -> bool:
        return self.tenant_uuid == MasterTenantProxy.get_master_tenant()

    @classmethod
    def from_token(cls, token: dict):
        if 'metadata' not in token:
            raise InvalidTokenError('Malformed token received, missing token details')
        return cls(token)

    @property
    def session_uuid(self) -> str:
        return self._token['session_uuid']

    @property
    def tenant_uuid(self) -> str:
        return self._token['metadata']['tenant_uuid']

    @property
    def token_id(self) -> str:
        return self._token['token']

    @property
    def token_utc_expires_at(self) -> str:
        return self._token['utc_expires_at']

    @property
    def uuid(self) -> str:
        return self._token['metadata']['uuid']


class _BusConnection:
    _id_counter = Value('i', 1)

    def __init__(self, url: str, *, loop: asyncio.AbstractEventLoop = None):
        self._id: int = self._get_unique_id()
        self._url: str = url
        self._loop = loop or asyncio.get_event_loop()
        self._closing = asyncio.Event()
        self._connected = asyncio.Event()
        self._consumers: list[BusConsumer] = []
        self._protocol: AmqpProtocol = None
        self._transport = None
        self._task: asyncio.Task = None

    @classmethod
    def _get_unique_id(cls) -> int:
        id_ = cls._id_counter.value
        cls._id_counter.value += 1
        return id_

    @property
    def is_closing(self):
        return self._closing.is_set()

    @property
    def is_connected(self):
        return self._connected.is_set()

    async def run(self):
        while True:
            if not await self.connect():
                return

            # Wait for the connection to terminate
            await self._protocol.wait_closed()
            self._transport.close()
            self._connected.clear()

            # Notify consumers of disconnection
            await self._notify_closed()

            # if terminated, exit
            if self.is_closing:
                logger.info('[connection %d] connection to bus closed', self._id)
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
                try:
                    await asyncio.wait_for(self._closing.wait(), timeout)
                except asyncio.TimeoutError:
                    continue
                logger.info('[connection %d] cancelling connection...', self._id)
                self._closing.set()
                return False
            else:
                self._transport, self._protocol = transport, protocol
                self._connected.set()
                logger.info('[connection %d] connected to bus', self._id)
                return True

    async def disconnect(self):
        self._closing.set()
        if self.is_connected:
            await self._protocol.close()
            self._transport.close()

    async def get_channel(self, *, wait: bool = True) -> Channel:
        if not self.is_connected and not wait:
            raise BusConnectionError(
                f'[connection {self._id}] connection isn\'t established yet'
            )

        try:
            await self._wait_for_connection()
            return await self._protocol.channel()
        except (AmqpClosedConnection, BusConnectionError, ChannelClosed):
            raise BusConnectionError(
                f'[connection {self._id}] failed to create a new channel'
            )

    def spawn_consumer(self, config: dict, token: str) -> BusConsumer:
        consumer = BusConsumer(self, config, token)
        self._consumers.append(consumer)
        return consumer

    def remove_consumer(self, consumer):
        if consumer not in self._consumers:
            raise ValueError('consumer does not belong to this connection')
        self._consumers.remove(consumer)

    async def _notify_closed(self):
        tasks = [consumer.connection_lost() for consumer in self._consumers]
        await asyncio.gather(*tasks, loop=self._loop)

    async def _wait_for_connection(self):
        futs = [self._closing.wait(), self._connected.wait()]
        await asyncio.wait(futs, return_when=asyncio.FIRST_COMPLETED)
        if self.is_closing:
            raise BusConnectionError(f'[connection {self._id}] connection is closing')


class _BusConnectionPool:
    def __init__(self, url: str, pool_size: int, *, loop=None):
        self._loop = loop or asyncio.get_event_loop()
        self._connections = [_BusConnection(url, loop=loop) for _ in range(pool_size)]
        self._tasks = set()
        self._iterator = cycle(self._connections)

    def __len__(self):
        return len(self._connections)

    async def start(self):
        self._tasks = {
            self._loop.create_task(connection.run()) for connection in self._connections
        }
        logger.info('bus connection pool initialized with %d connections', len(self))

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

        logger.info('bus connection pool closed (%s connections)', len(self))

    def get_connection(self):
        return next(self._iterator)


class BusConsumer:
    def __init__(self, connection: _BusConnection, config: dict, token: str):
        self.set_token(token)
        self._amqp_queue: str | None = None
        self._bound_exchange: str | None = None
        self._channel: Channel = None
        self._connection: _BusConnection = connection
        self._consumer_tag: str | None = None
        self._exchange_name: str = config['bus']['exchange_name']
        self._prefetch: int = config['bus']['consumer_prefetch']
        self._origin_uuid: str = config['uuid']
        self._queue = asyncio.Queue()

    async def __aenter__(self):
        await self._start_consuming()
        return self

    async def __aexit__(self, *args):
        await self._stop_consuming()

    def __aiter__(self):
        return self

    async def __anext__(self) -> BusMessage:
        payload = await self._queue.get()
        if isinstance(payload, Exception):
            raise payload
        return payload

    async def _consume_queue(self, channel: Channel, queue_name: str) -> str:
        response = await channel.basic_consume(
            self._on_message, queue_name, exclusive=True
        )
        if response['consumer_tag'] is None:
            raise BusConnectionError
        return response['consumer_tag']

    async def _create_tenant_exchange(self, channel: Channel, exchange: str) -> str:
        tenant_uuid = self._user.tenant_uuid
        tenant_exchange = self._generate_name(f'tenant-{tenant_uuid}')

        await channel.exchange(
            tenant_exchange, 'headers', durable=False, auto_delete=True
        )

        await channel.exchange_bind(
            tenant_exchange,
            exchange,
            '',
            arguments={'origin_uuid': self._origin_uuid, 'tenant_uuid': tenant_uuid},
        )

        return tenant_exchange

    async def _create_queue(self, channel: Channel, prefetch: int = 1) -> str:
        queue_name = self._generate_name(f'user-{self._user.uuid}', token_hex(3))

        await channel.basic_qos(prefetch_count=prefetch, connection_global=False)

        response = await channel.queue(
            queue_name, durable=False, auto_delete=True, exclusive=True
        )
        if response['queue'] is None:
            raise BusConnectionError
        return response['queue']

    def _decode_content(self, content: bytes, properties: Properties) -> BusMessage:
        headers = properties.headers
        try:
            decoded = content.decode('utf-8')
            message = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InvalidEvent('unable to decode message')

        if not isinstance(message, dict):
            raise InvalidEvent('invalid message format (not a dict)')

        event_name = headers.get('name') or message.get('name')
        if not event_name:
            raise InvalidEvent('event is missing `name` field')

        if 'required_acl' not in headers:
            raise EventPermissionError(f'event `{event_name}` doesn\'t contain ACLs`')
        acl = headers.get('required_acl')

        if isinstance(acl, bytes):
            acl = acl.decode('utf-8')
        if acl and not isinstance(acl, str):
            raise InvalidEvent(
                'event ACL is not a string (type: %s)', type(acl).__name__
            )

        if not self._has_access(acl):
            raise EventPermissionError(
                f'user `{self._user.uuid}` doesn\'t have the required ACL for event `{event_name}` (missing: {acl})'
            )

        return BusMessage(event_name, headers, acl, message, decoded)

    def _generate_bindings(self, event_name: str) -> list[dict]:
        binding = {}
        if event_name != '*':
            binding['name'] = event_name

        if self._user.is_admin():
            binding['origin_uuid'] = self._origin_uuid
            return [binding]

        # note: users don't need origin_uuid because the tenant exchange takes care of it
        return [
            binding | {f'user_uuid:{self._user.uuid}': True},
            binding | {'user_uuid:*': True},
        ]

    def _has_access(self, acl: str) -> bool:
        return self._access.matches_required_access(acl)

    async def _on_message(
        self,
        channel: Channel,
        content: bytes,
        envelope: Envelope,
        properties: Properties,
    ) -> None:
        try:
            event = self._decode_content(content, properties)
        except InvalidEvent as exc:
            logger.error('error during message decoding (reason: %s)', exc)
        except EventPermissionError as exc:
            logger.debug('discarding event (reason: %s)', exc)
        else:
            self._queue.put_nowait(event)
        finally:
            await channel.basic_client_ack(envelope.delivery_tag, multiple=True)

    async def _start_consuming(self) -> None:
        channel = self._channel = await self._connection.get_channel(wait=False)
        exchange = self._exchange_name

        if not self._user.is_master_tenant():
            exchange = await self._create_tenant_exchange(channel, self._exchange_name)
        self._bound_exchange = exchange

        # Create exclusive queue on exchange
        self._amqp_queue = await self._create_queue(channel, self._prefetch)

        # Start consuming on queue
        self._consumer_tag = await self._consume_queue(channel, self._amqp_queue)

        if self._user.is_master_tenant():
            logger.debug('user `%s` connected as global admin', self._user.uuid)
        elif self._user.is_admin():
            logger.debug('user `%s` connected as tenant\'s admin', self._user.uuid)
        else:
            logger.debug('user `%s` connected as user', self._user.uuid)

    async def _stop_consuming(self) -> None:
        if self._channel.is_open:
            if self._consumer_tag is not None:
                await self._channel.basic_cancel(self._consumer_tag)
            await self._channel.close()
        self._connection.remove_consumer(self)

    async def bind(self, event_name: str) -> None:
        for binding in self._generate_bindings(event_name):
            await self._channel.queue_bind(
                self._amqp_queue, self._bound_exchange, '', arguments=binding
            )

    async def connection_lost(self) -> None:
        self._queue.put_nowait(BusConnectionLostError())

    async def unbind(self, event_name: str) -> None:
        for binding in self._generate_bindings(event_name):
            await self._channel.queue_unbind(
                self._amqp_queue, self._bound_exchange, '', arguments=binding
            )

    def get_token(self) -> dict[str, str]:
        return {
            'token': self._user.token_id,
            'utc_expires_at': self._user.token_utc_expires_at,
        }

    def set_token(self, token: dict):
        self._user = user = _UserHelper.from_token(token)
        self._access = AccessCheck(user.uuid, user.session_uuid, user.acl)

    @staticmethod
    def _generate_name(*parts: str) -> str:
        return '.'.join(['wazo-websocketd', *parts])


class BusMessage(NamedTuple):
    name: str
    headers: dict
    acl: str
    content: dict
    raw: str


class BusService:
    def __init__(self, config: dict, *, loop: asyncio.AbstractEventLoop = None):
        poolsize: int = config.get('worker_connections', 1)
        url: str = 'amqp://{username}:{password}@{host}:{port}//'.format(
            **config['bus']
        )

        self._config = config
        self._loop = loop or asyncio.get_event_loop()
        self._connection_pool = _BusConnectionPool(url, poolsize, loop=loop)

    async def __aenter__(self):
        await self._connection_pool.start()
        return self

    async def __aexit__(self, *args):
        await self._connection_pool.stop()

    async def create_consumer(self, token: str) -> BusConsumer:
        connection = self._connection_pool.get_connection()
        return connection.spawn_consumer(self._config, token)

    async def initialize_exchanges(self):
        async def create_exchange(config: dict, channel: Channel):
            name: str = config['bus']['exchange_name']
            type_: str = config['bus']['exchange_type']
            await channel.exchange(name, type_, durable=True)
            logger.info('exchange `%s` initialized', name)

        # Migration <22.13
        # Upgrading from a previous version will keep `wazo-websocketd` exchange
        # since it is durable, but is no longer needed, so let's delete it if unused
        async def remove_deprecated(config: dict, channel: Channel):
            if config['bus']['exchange_name'] != 'wazo-websocketd':
                await channel.exchange_delete('wazo-websocketd', if_unused=True)
                logger.info('migration: removed legacy `wazo-websocketd` exchange...')

        logger.info('configuring RabbitMQ for wazo-websocketd...')
        connection = self._connection_pool.get_connection()
        try:
            channel = await connection.get_channel(wait=True)
        except BusConnectionError:
            return

        await create_exchange(self._config, channel)
        await remove_deprecated(self._config, channel)
        await channel.close()
