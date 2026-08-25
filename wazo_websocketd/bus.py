# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from itertools import cycle
from multiprocessing import Value
from typing import Any

import aioamqp
from aioamqp import AmqpProtocol
from aioamqp.channel import Channel
from aioamqp.envelope import Envelope
from aioamqp.exceptions import AmqpClosedConnection, ChannelClosed
from aioamqp.properties import Properties

from .exception import BusConnectionError, BusConnectionLostError
from .helpers.timing import capped_backoff

logger = logging.getLogger(__name__)

SetupHook = Callable[[Channel], Awaitable[None]]
DeliveryHandler = Callable[[bytes, Properties], Any]


def generate_name(*parts: str) -> str:
    return '.'.join(['wazo-websocketd', *parts])


def bus_url(config: dict) -> str:
    return 'amqp://{username}:{password}@{host}:{port}/{vhost}'.format(**config['bus'])


class BusConnection:
    _id_counter = Value('i', 1)

    def __init__(self, url: str):
        self._id: int = self._get_unique_id()
        self._url: str = url
        self._closing = asyncio.Event()
        self._connected = asyncio.Event()
        self._consumers: list[_BusConsumer] = []
        self._protocol: AmqpProtocol = None  # type: ignore[assignment]
        self._transport: asyncio.Transport = None  # type: ignore[assignment]
        self._task: asyncio.Task = None  # type: ignore[assignment]

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
        timeouts = capped_backoff(1, 5, 32)
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

    def _add_consumer(self, consumer: _BusConsumer) -> None:
        self._consumers.append(consumer)

    def _remove_consumer(self, consumer: _BusConsumer) -> None:
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
            raise BusConnectionError(f'[connection {self._id}] connection is closing')


class _BusConnectionPool:
    def __init__(self, url: str, pool_size: int):
        self._connections = [BusConnection(url) for _ in range(pool_size)]
        self._tasks: set = set()
        self._iterator = cycle(self._connections)

    def __len__(self):
        return len(self._connections)

    async def start(self):
        self._tasks = {
            asyncio.create_task(connection.run()) for connection in self._connections
        }
        logger.info('bus connection pool initialized with %d connections', len(self))

    async def stop(self):
        await asyncio.gather(
            *{
                asyncio.create_task(connection.disconnect())
                for connection in self._connections
            }
        )

        if not self._tasks:  # never started, so nothing to wind down
            return

        # wait for connections to close gracefully or force after 5 sec
        _, pending = await asyncio.wait(self._tasks, timeout=5.0)

        if pending:
            logger.info('some connections did not exit gracefully, forcing...')
            for task in pending:
                task.cancel()

        logger.info('bus connection pool closed (%s connections)', len(self))

    def get_connection(self):
        return next(self._iterator)


class _BusConsumer:
    """Consumes an exclusive queue on the bus, whatever it is bound for."""

    _PREFETCH = 1

    def __init__(
        self,
        connection: BusConnection,
        *,
        queue_name: str,
        exchange: str,
        handler: DeliveryHandler,
        wait: bool,
        bindings: Sequence[dict] = (),
        prefetch: int = _PREFETCH,
        on_setup: SetupHook | None = None,
    ):
        self._amqp_queue: str | None = None
        self._channel: Channel = None
        self._connection: BusConnection = connection
        self._consumer_tag: str | None = None
        self._bound_exchange = exchange
        self._prefetch = prefetch
        self._queue: asyncio.Queue = asyncio.Queue()
        self._queue_name = queue_name
        self._handler = handler
        self._wait = wait
        self._bindings = bindings
        self._on_setup = on_setup

    async def __aenter__(self):
        # a sentinel from a previous connection would abort this one at once
        self._queue = asyncio.Queue()
        self._connection._add_consumer(self)
        try:
            await self._start_consuming()
        except Exception:
            await self._discard_channel()
            self._connection._remove_consumer(self)
            raise
        return self

    async def __aexit__(self, *args):
        try:
            await self._stop_consuming()
        finally:
            self._connection._remove_consumer(self)

    async def _discard_channel(self) -> None:
        if self._channel is not None and self._channel.is_open:
            with contextlib.suppress(AmqpClosedConnection, ChannelClosed, OSError):
                await self._channel.close()
        self._channel = None

    def __aiter__(self):
        return self

    async def __anext__(self) -> Any:
        payload = await self._queue.get()
        if isinstance(payload, Exception):
            raise payload
        return payload

    async def connection_lost(self) -> None:
        self._queue.put_nowait(BusConnectionLostError())

    async def bind(self, arguments: dict) -> None:
        await self._channel.queue_bind(
            self._amqp_queue, self._bound_exchange, '', arguments=arguments
        )

    async def unbind(self, arguments: dict) -> None:
        await self._channel.queue_unbind(
            self._amqp_queue, self._bound_exchange, '', arguments=arguments
        )

    async def _start_consuming(self) -> None:
        self._channel = await self._connection.get_channel(wait=self._wait)

        if self._on_setup is not None:
            await self._on_setup(self._channel)

        self._amqp_queue = await self._create_queue()
        for arguments in self._bindings:
            await self.bind(arguments)
        self._consumer_tag = await self._consume_queue()

    async def _stop_consuming(self) -> None:
        if self._channel.is_open:
            if self._consumer_tag is not None:
                await self._channel.basic_cancel(self._consumer_tag)
            await self._channel.close()

    async def _create_queue(self) -> str:
        await self._channel.basic_qos(
            prefetch_count=self._prefetch, connection_global=False
        )

        response = await self._channel.queue(
            self._queue_name, durable=False, auto_delete=True, exclusive=True
        )
        if response['queue'] is None:
            raise BusConnectionError
        return response['queue']

    async def _consume_queue(self) -> str:
        response = await self._channel.basic_consume(
            self._on_message, self._amqp_queue, exclusive=True
        )
        if response['consumer_tag'] is None:
            raise BusConnectionError
        return response['consumer_tag']

    async def _on_message(
        self,
        channel: Channel,
        content: bytes,
        envelope: Envelope,
        properties: Properties,
    ) -> None:
        try:
            if (payload := self._handler(content, properties)) is not None:
                self._queue.put_nowait(payload)
        finally:
            await channel.basic_client_ack(envelope.delivery_tag, multiple=True)


class BusService:
    def __init__(self, config: dict):
        poolsize: int = config.get('worker_connections', 1)

        self._config = config
        self._connection_pool = _BusConnectionPool(bus_url(config), poolsize)

    async def start(self) -> None:
        await self._connection_pool.start()

    async def stop(self) -> None:
        await self._connection_pool.stop()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    def connection(self) -> BusConnection:
        return self._connection_pool.get_connection()

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
