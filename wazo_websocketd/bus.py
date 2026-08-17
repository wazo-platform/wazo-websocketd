# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import aioamqp
from aioamqp import AmqpProtocol
from aioamqp.channel import Channel
from aioamqp.envelope import Envelope
from aioamqp.exceptions import AmqpClosedConnection, ChannelClosed
from aioamqp.properties import Properties

from .exception import BusConnectionError, BusConnectionLostError
from .helpers.timing import capped_backoff, exponential_backoff

logger = logging.getLogger(__name__)

_AMQP_NOT_FOUND = 404

SetupHook = Callable[[Channel], Awaitable[None]]
DeliveryHandler = Callable[[bytes, Properties], Any]


def generate_name(*parts: str) -> str:
    return '.'.join(['wazo-websocketd', *parts])


def bus_url(config: dict) -> str:
    return 'amqp://{username}:{password}@{host}:{port}/{vhost}'.format(**config['bus'])


class BusConnection:
    def __init__(self, url: str):
        self._url: str = url
        self._closing = asyncio.Event()
        self._connected = asyncio.Event()
        self._consumers: list[_BusConsumer] = []
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
        timeouts = capped_backoff(1, 5, 32)
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
            raise BusConnectionError('bus connection is closing')


class _BusConsumer:
    """Consumes an exclusive queue on the bus, whatever it is bound for."""

    _PREFETCH = 1
    _SETUP_DELAY = 0.05
    _SETUP_RETRIES = 4

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

    async def bind(self, arguments: dict, *, no_wait: bool = False) -> None:
        await self._channel.queue_bind(
            self._amqp_queue,
            self._bound_exchange,
            '',
            no_wait=no_wait,
            arguments=arguments,
        )

    async def unbind(self, arguments: dict) -> None:
        await self._channel.queue_unbind(
            self._amqp_queue, self._bound_exchange, '', arguments=arguments
        )

    async def _start_consuming(self) -> None:
        for delay in exponential_backoff(self._SETUP_DELAY, self._SETUP_RETRIES):
            try:
                await self._declare_topology()
                return
            except ChannelClosed as e:
                if e.code != _AMQP_NOT_FOUND:
                    raise
                logger.info(
                    'bus topology vanished while connecting (%s), retrying', e.message
                )
                await self._discard_channel()
                await asyncio.sleep(delay)

        await self._declare_topology()

    async def _declare_topology(self) -> None:
        self._channel = await self._connection.get_channel(wait=self._wait)

        if self._on_setup is not None:
            await self._on_setup(self._channel)

        self._amqp_queue = await self._create_queue()
        for arguments in self._bindings:
            await self.bind(arguments, no_wait=True)
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

        await self._channel.queue(
            self._queue_name,
            durable=False,
            auto_delete=True,
            exclusive=True,
            no_wait=True,
        )
        return self._queue_name

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

    async def start(self) -> None:
        self._task = asyncio.create_task(self._connection.run())

    async def stop(self) -> None:
        await self._connection.disconnect()

        if self._task is None:  # never started, so nothing to wind down
            return

        _, pending = await asyncio.wait([self._task], timeout=self._STOP_TIMEOUT)
        if pending:
            logger.info('bus connection did not exit gracefully, forcing...')
            self._task.cancel()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    def connection(self) -> BusConnection:
        return self._connection

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
