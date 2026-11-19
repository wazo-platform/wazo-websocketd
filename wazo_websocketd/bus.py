# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import json
import logging
from itertools import chain, cycle, repeat
from multiprocessing import Value
from secrets import token_hex
from typing import NamedTuple

import aioamqp
from aioamqp import AmqpProtocol
from aioamqp.channel import Channel
from aioamqp.envelope import Envelope
from aioamqp.exceptions import AmqpClosedConnection, ChannelClosed
from aioamqp.properties import Properties
from wazo_auth_client.types import TokenDict
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

TENANT_BROADCAST = {'user_uuid:*': True}


def _tenant_exchange_name(tenant_uuid: str) -> str:
    return BusConsumer._generate_name(f'tenant-{tenant_uuid}')


async def _declare_tenant_exchange(
    channel: Channel, upstream: str, tenant_uuid: str, origin_uuid: str
) -> str:
    exchange = _tenant_exchange_name(tenant_uuid)
    await channel.exchange(exchange, 'headers', durable=False, auto_delete=True)
    await channel.exchange_bind(
        exchange,
        upstream,
        '',
        arguments={'origin_uuid': origin_uuid, 'tenant_uuid': tenant_uuid},
    )
    return exchange


class _UserHelper:
    def __init__(self, token: TokenDict):
        self._token = token

    @property
    def acl(self) -> list[str]:
        return self._token['acl']

    def is_admin(self) -> bool:
        purpose = self._token['metadata'].get('purpose', None)
        is_tenant_admin = bool(self._token['metadata'].get('admin', False))
        return (
            self.is_master_tenant()
            or is_tenant_admin
            or purpose in ('external_api', 'internal')
        )

    def is_master_tenant(self) -> bool:
        return self.tenant_uuid == MasterTenantProxy.get_master_tenant()

    @classmethod
    def from_token(cls, token: TokenDict):
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

    def __init__(self, url: str):
        self._id: int = self._get_unique_id()
        self._url: str = url
        self._closing = asyncio.Event()
        self._connected = asyncio.Event()
        self._consumers: list[BusConsumer] = []
        self._protocol: AmqpProtocol = None  # type: ignore[assignment]
        self._transport: asyncio.Transport = None  # type: ignore[assignment]
        self._task: asyncio.Task = None  # type: ignore[assignment]
        self._tenant_channel: Channel | None = None
        self._tenant_queue: str | None = None
        self._tenant_lock = asyncio.Lock()

    @classmethod
    def _get_unique_id(cls) -> int:
        id_ = cls._id_counter.value  # type: ignore[attr-defined]
        cls._id_counter.value += 1  # type: ignore[attr-defined]
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
            self._tenant_channel = None

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
                except TimeoutError:
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

    def spawn_consumer(self, config: dict, token: TokenDict) -> BusConsumer:
        consumer = BusConsumer(self, config, token)
        self._consumers.append(consumer)
        return consumer

    def remove_consumer(self, consumer):
        if consumer not in self._consumers:
            raise ValueError('consumer does not belong to this connection')
        self._consumers.remove(consumer)

    async def acquire_tenant_exchange(
        self, tenant_uuid: str, upstream: str, origin_uuid: str, prefetch: int
    ) -> str:
        async with self._tenant_lock:
            for _ in range(3):
                subscribed = {t for c in self._consumers if (t := c._subscribed_tenant)}
                try:
                    if self._tenant_channel is None or not self._tenant_channel.is_open:
                        await self._open_tenant_queue(prefetch)
                        for tenant in subscribed | {tenant_uuid}:
                            await self._bind_tenant(tenant, upstream, origin_uuid)
                    elif tenant_uuid not in subscribed:
                        await self._bind_tenant(tenant_uuid, upstream, origin_uuid)
                    return _tenant_exchange_name(tenant_uuid)
                except ChannelClosed as exc:
                    logger.info(
                        '[connection %d] tenant queue channel closed (%s), retrying',
                        self._id,
                        exc,
                    )
                    self._tenant_channel = None
            raise BusConnectionError(
                f'[connection {self._id}] unable to bind tenant `{tenant_uuid}`'
            )

    async def _open_tenant_queue(self, prefetch: int) -> None:
        channel = await self.get_channel(wait=False)
        await channel.basic_qos(prefetch_count=prefetch, connection_global=False)
        queue = BusConsumer._generate_name('tenants', token_hex(3))
        await channel.queue(queue, durable=False, auto_delete=True, exclusive=True)
        await channel.basic_consume(self._on_tenant_message, queue, exclusive=True)
        self._tenant_channel, self._tenant_queue = channel, queue

    async def _bind_tenant(self, tenant_uuid: str, upstream: str, origin: str) -> None:
        assert self._tenant_channel is not None
        exchange = await _declare_tenant_exchange(
            self._tenant_channel, upstream, tenant_uuid, origin
        )
        await self._tenant_channel.queue_bind(
            self._tenant_queue, exchange, '', arguments=TENANT_BROADCAST
        )

    async def release_tenant_exchange(self, consumer: BusConsumer) -> None:
        tenant_uuid = consumer._subscribed_tenant
        if tenant_uuid is None:
            return
        async with self._tenant_lock:
            if any(
                c is not consumer and c._subscribed_tenant == tenant_uuid
                for c in self._consumers
            ):
                return
            if self._tenant_channel is None or not self._tenant_channel.is_open:
                return
            try:
                await self._tenant_channel.queue_unbind(
                    self._tenant_queue,
                    _tenant_exchange_name(tenant_uuid),
                    '',
                    arguments=TENANT_BROADCAST,
                )
            except (AmqpClosedConnection, ChannelClosed):
                pass

    async def _on_tenant_message(
        self,
        channel: Channel,
        content: bytes,
        envelope: Envelope,
        properties: Properties,
    ) -> None:
        try:
            tenant_uuid = (properties.headers or {}).get('tenant_uuid')
            if isinstance(tenant_uuid, bytes):
                tenant_uuid = tenant_uuid.decode('utf-8', 'replace')
            for consumer in self._consumers:
                if consumer._subscribed_tenant != tenant_uuid:
                    continue
                try:
                    consumer._handle_message(content, properties)
                except Exception:
                    logger.exception('error while dispatching tenant event')
        finally:
            await channel.basic_client_ack(envelope.delivery_tag, multiple=True)

    async def _notify_closed(self):
        tasks = [consumer.connection_lost() for consumer in self._consumers]
        await asyncio.gather(*tasks)

    async def _wait_for_connection(self):
        futs = [
            asyncio.create_task(self._closing.wait()),
            asyncio.create_task(self._connected.wait()),
        ]
        await asyncio.wait(futs, return_when=asyncio.FIRST_COMPLETED)
        if self.is_closing:
            raise BusConnectionError(f'[connection {self._id}] connection is closing')


class _BusConnectionPool:
    def __init__(self, url: str, pool_size: int):
        self._loop = asyncio.get_event_loop()
        self._connections = [_BusConnection(url) for _ in range(pool_size)]
        self._tasks: set = set()
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
            *{
                asyncio.create_task(connection.disconnect())
                for connection in self._connections
            }
        )

        # wait for connections to close gracefully or force after 5 sec
        _, pending = await asyncio.wait(self._tasks, timeout=5.0)

        if pending:
            logger.info('some connections did not exit gracefully, forcing...')
            for task in pending:
                task.cancel()

        logger.info('bus connection pool closed (%s connections)', len(self))

    def get_connection(self):
        return next(self._iterator)


class BusConsumer:
    def __init__(self, connection: _BusConnection, config: dict, token: TokenDict):
        self.set_token(token)
        self._amqp_queue: str | None = None
        self._bound_exchange: str | None = None
        self._channel: Channel = None
        self._connection: _BusConnection = connection
        self._consumer_tag: str | None = None
        self._exchange_name: str = config['bus']['exchange_name']
        self._prefetch: int = config['bus']['consumer_prefetch']
        self._origin_uuid: str = config['uuid']
        self._queue: asyncio.Queue = asyncio.Queue()
        self._event_names: set[str] = set()
        self._wildcard_bound: bool = False
        self._subscribed_tenant: str | None = None

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
        return await _declare_tenant_exchange(
            channel, exchange, self._user.tenant_uuid, self._origin_uuid
        )

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
                f'user `{self._user.uuid}` doesn\'t have '
                f'the required ACL for event `{event_name}` (missing: {acl})'
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
            self._handle_message(content, properties)
        finally:
            await channel.basic_client_ack(envelope.delivery_tag, multiple=True)

    def _handle_message(self, content: bytes, properties: Properties) -> None:
        try:
            name = (properties.headers or {}).get('name')
            if isinstance(name, bytes):
                name = name.decode('utf-8', 'replace')
            if name and not self._is_subscribed(name):
                return
            event = self._decode_content(content, properties)
        except InvalidEvent as exc:
            logger.error('error during message decoding (reason: %s)', exc)
        except EventPermissionError as exc:
            logger.debug('discarding event (reason: %s)', exc)
        else:
            if self._is_subscribed(event.name):
                self._queue.put_nowait(event)

    async def _start_consuming(self) -> None:
        channel = self._channel = await self._connection.get_channel(wait=False)
        exchange = self._exchange_name

        if not self._user.is_admin():
            exchange = await self._connection.acquire_tenant_exchange(
                self._user.tenant_uuid,
                self._exchange_name,
                self._origin_uuid,
                self._prefetch,
            )
            self._subscribed_tenant = self._user.tenant_uuid
        elif not self._user.is_master_tenant():
            exchange = await self._create_tenant_exchange(channel, self._exchange_name)
        self._bound_exchange = exchange

        try:
            self._amqp_queue = await self._create_queue(channel, self._prefetch)
            self._consumer_tag = await self._consume_queue(channel, self._amqp_queue)
        except BaseException:
            await self._connection.release_tenant_exchange(self)
            self._subscribed_tenant = None
            raise

        if self._user.is_master_tenant():
            logger.debug('user `%s` connected as global admin', self._user.uuid)
        elif self._user.is_admin():
            logger.debug('user `%s` connected as tenant\'s admin', self._user.uuid)
        else:
            logger.debug('user `%s` connected as user', self._user.uuid)

    async def _stop_consuming(self) -> None:
        await self._connection.release_tenant_exchange(self)
        self._subscribed_tenant = None
        if self._channel.is_open:
            if self._consumer_tag is not None:
                await self._channel.basic_cancel(self._consumer_tag)
            await self._channel.close()
        self._connection.remove_consumer(self)

    def _is_subscribed(self, event_name: str) -> bool:
        if self._user.is_admin():
            return True
        return '*' in self._event_names or event_name in self._event_names

    async def bind(self, event_name: str) -> None:
        if not self._user.is_admin():
            self._event_names.add(event_name)
            if self._wildcard_bound:
                return
            self._wildcard_bound = True
            await self._channel.queue_bind(
                self._amqp_queue,
                self._bound_exchange,
                '',
                arguments={f'user_uuid:{self._user.uuid}': True},
            )
            return

        for binding in self._generate_bindings(event_name):
            await self._channel.queue_bind(
                self._amqp_queue, self._bound_exchange, '', arguments=binding
            )

    async def connection_lost(self) -> None:
        self._queue.put_nowait(BusConnectionLostError())

    async def unbind(self, event_name: str) -> None:
        if not self._user.is_admin():
            self._event_names.discard(event_name)
            return
        for binding in self._generate_bindings(event_name):
            await self._channel.queue_unbind(
                self._amqp_queue, self._bound_exchange, '', arguments=binding
            )

    def get_token(self) -> dict[str, str]:
        return {
            'token': self._user.token_id,
            'utc_expires_at': self._user.token_utc_expires_at,
        }

    def set_token(self, token: TokenDict):
        self._user = user = _UserHelper.from_token(token)
        self._access = AccessCheck(user.uuid, user.session_uuid, user.acl)

    @staticmethod
    def _generate_name(*parts: str) -> str:
        return '.'.join(['wazo-websocketd', *parts])


class BusMessage(NamedTuple):
    name: str
    headers: dict
    acl: str | None
    content: dict
    raw: str


class BusService:
    def __init__(self, config: dict):
        poolsize: int = config.get('worker_connections', 1)
        url: str = 'amqp://{username}:{password}@{host}:{port}//'.format(
            **config['bus']
        )

        self._config = config
        self._connection_pool = _BusConnectionPool(url, poolsize)

    async def __aenter__(self):
        await self._connection_pool.start()
        return self

    async def __aexit__(self, *args):
        await self._connection_pool.stop()

    async def create_consumer(self, token: TokenDict) -> BusConsumer:
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
