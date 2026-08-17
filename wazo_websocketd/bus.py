# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import json
import logging
from itertools import chain, repeat
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

from .auth import MasterTenant
from .exception import (
    AuthenticationError,
    BusConnectionError,
    BusConnectionLostError,
    EventPermissionError,
    InvalidEvent,
    InvalidTokenError,
    SessionRevokedError,
)
from .helpers.timing import exponential_backoff

logger = logging.getLogger(__name__)

SESSION_DELETED_EVENT = 'auth_session_deleted'
_NOT_FOUND = 404


def parse_event_session_uuid(body: bytes | str) -> str | None:
    try:
        message = json.loads(body)
        data = message.get('data') or message
        return data['uuid']
    except (ValueError, KeyError, TypeError, AttributeError):
        logger.warning('discarding malformed `%s` event', SESSION_DELETED_EVENT)
        return None


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
        return MasterTenant.matches(self.tenant_uuid)

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


def bus_url(config: dict) -> str:
    return 'amqp://{username}:{password}@{host}:{port}//'.format(**config['bus'])


class BusConnection:
    def __init__(self, url: str):
        self._url: str = url
        self._closing = asyncio.Event()
        self._connected = asyncio.Event()
        self._consumers: list[BusConsumer] = []
        self._protocol: AmqpProtocol = None  # type: ignore[assignment]
        self._transport: asyncio.Transport = None  # type: ignore[assignment]
        self._task: asyncio.Task = None  # type: ignore[assignment]

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
                logger.info('bus connection closed')
                return

            logger.info('bus connection lost, attempting to reconnect...')

    async def connect(self):
        timeouts = chain(exponential_backoff(1, 5), repeat(32))
        while True:
            try:
                transport, protocol = await aioamqp.from_url(self._url, heartbeat=10)
            except (AmqpClosedConnection, OSError):
                timeout = next(timeouts)
                logger.debug(
                    'unable to connect to the bus, retrying in %d seconds', timeout
                )
                try:
                    await asyncio.wait_for(self._closing.wait(), timeout)
                except TimeoutError:
                    continue
                logger.info('cancelling bus connection...')
                self._closing.set()
                return False
            else:
                self._transport, self._protocol = transport, protocol
                self._connected.set()
                logger.info('connected to bus')
                return True

    async def disconnect(self):
        self._closing.set()
        if self.is_connected:
            await self._protocol.close()
            self._transport.close()

    async def get_channel(self, *, wait: bool = True) -> Channel:
        if not self.is_connected and not wait:
            raise BusConnectionError('bus connection is not established yet')

        try:
            await self._wait_for_connection()
            return await self._protocol.channel()
        except (AmqpClosedConnection, BusConnectionError, ChannelClosed):
            raise BusConnectionError('failed to create a new bus channel')

    def spawn_consumer(self, config: dict, token: TokenDict) -> BusConsumer:
        return BusConsumer(self, config, token)

    def add_consumer(self, consumer) -> None:
        self._consumers.append(consumer)

    def remove_consumer(self, consumer):
        if consumer not in self._consumers:
            raise ValueError('consumer does not belong to this connection')
        self._consumers.remove(consumer)

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
            raise BusConnectionError('bus connection is closing')


class BusConsumer:
    _SETUP_DELAY = 0.05
    _SETUP_RETRIES = 4

    def __init__(self, connection: BusConnection, config: dict, token: TokenDict):
        user = _UserHelper.from_token(token)
        self._amqp_queue: str | None = None
        self._bound_exchange: str | None = None
        self._channel: Channel = None
        self._connection: BusConnection = connection
        self._consumer_tag: str | None = None
        self._exchange_name: str = config['bus']['exchange_name']
        self._prefetch: int = config['bus']['consumer_prefetch']
        self._origin_uuid: str = config['uuid']
        self._queue: asyncio.Queue = asyncio.Queue()
        self._subscriptions: set[str] = set()
        self._set_user(user)

    async def __aenter__(self):
        await self._start_consuming()
        self._connection.add_consumer(self)
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
            tenant_exchange, 'headers', durable=False, auto_delete=True, no_wait=True
        )

        await channel.exchange_bind(
            tenant_exchange,
            exchange,
            '',
            no_wait=True,
            arguments={'origin_uuid': self._origin_uuid, 'tenant_uuid': tenant_uuid},
        )

        return tenant_exchange

    async def _create_queue(self, channel: Channel, prefetch: int = 1) -> str:
        queue_name = self._generate_name(f'user-{self._user.uuid}', token_hex(3))

        await channel.basic_qos(prefetch_count=prefetch, connection_global=False)

        await channel.queue(
            queue_name,
            durable=False,
            auto_delete=True,
            exclusive=True,
            no_wait=True,
        )
        return queue_name

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
            if self._is_revocation(content, properties):
                logger.info('session was deleted, closing the connection')
                self._queue.put_nowait(SessionRevokedError())
                return

            event = self._decode_content(content, properties)
        except InvalidEvent as exc:
            logger.error('error during message decoding (reason: %s)', exc)
        except EventPermissionError as exc:
            logger.debug('discarding event (reason: %s)', exc)
        else:
            if self._is_subscribed(event.name):
                self._queue.put_nowait(event)
        finally:
            await channel.basic_client_ack(envelope.delivery_tag, multiple=True)

    def _is_revocation(self, content: bytes, properties: Properties) -> bool:
        if (properties.headers or {}).get('name') != SESSION_DELETED_EVENT:
            return False
        return parse_event_session_uuid(content) == self._user.session_uuid

    def _is_subscribed(self, event_name: str) -> bool:
        return '*' in self._subscriptions or event_name in self._subscriptions

    async def _start_consuming(self) -> None:
        for delay in exponential_backoff(self._SETUP_DELAY, self._SETUP_RETRIES):
            try:
                await self._declare_topology()
                return
            except ChannelClosed as e:
                if e.code != _NOT_FOUND:
                    raise
                logger.info(
                    'bus topology vanished while connecting (%s), retrying', e.message
                )
                await asyncio.sleep(delay)

        await self._declare_topology()

    async def _declare_topology(self) -> None:
        channel = self._channel = await self._connection.get_channel(wait=False)
        self._amqp_queue = await self._create_queue(channel, self._prefetch)

        exchange = self._exchange_name
        if not self._user.is_master_tenant():
            exchange = await self._create_tenant_exchange(channel, self._exchange_name)
        self._bound_exchange = exchange

        # not a client subscription: the session must hear its own revocation
        await channel.queue_bind(
            self._amqp_queue,
            self._bound_exchange,
            '',
            no_wait=True,
            arguments={
                'name': SESSION_DELETED_EVENT,
                f'user_uuid:{self._user.uuid}': True,
            },
        )

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
        self._subscriptions.add(event_name)
        await self._bind(event_name)

    async def _bind(self, event_name: str) -> None:
        for binding in self._generate_bindings(event_name):
            await self._channel.queue_bind(
                self._amqp_queue, self._bound_exchange, '', arguments=binding
            )

    async def connection_lost(self) -> None:
        self._queue.put_nowait(BusConnectionLostError())

    async def unbind(self, event_name: str) -> None:
        self._subscriptions.discard(event_name)
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
        user = _UserHelper.from_token(token)
        if user.uuid != self._user.uuid:
            raise AuthenticationError('token belongs to another user')

        self._set_user(user)

    def _set_user(self, user: _UserHelper) -> None:
        self._user = user
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
    _STOP_TIMEOUT = 5.0

    def __init__(self, config: dict):
        if config.get('worker_connections') is not None:
            logger.warning(
                'configuration key `worker_connections` is deprecated and ignored: '
                'each worker process owns one bus connection'
            )

        self._config = config
        self._connection = BusConnection(bus_url(config))
        self._task: asyncio.Task = None  # type: ignore[assignment]

    async def __aenter__(self):
        self._task = asyncio.get_event_loop().create_task(self._connection.run())
        return self

    async def __aexit__(self, *args):
        await self._connection.disconnect()

        _, pending = await asyncio.wait([self._task], timeout=self._STOP_TIMEOUT)
        if pending:
            logger.info('bus connection did not exit gracefully, forcing...')
            self._task.cancel()

    async def create_consumer(self, token: TokenDict) -> BusConsumer:
        return self._connection.spawn_consumer(self._config, token)

    async def initialize_exchanges(self):
        try:
            channel = await self._connection.get_channel(wait=True)
        except BusConnectionError:
            return

        name: str = self._config['bus']['exchange_name']
        type_: str = self._config['bus']['exchange_type']
        await channel.exchange(name, type_, durable=True)
        await channel.close()
        logger.info('exchange `%s` initialized', name)
