# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qsl, urlparse

import websockets

from .auth import MasterTenantProxy
from .bus import BusConsumer, BusService
from .exception import (
    AuthenticationError,
    AuthenticationExpiredError,
    BusConnectionError,
    BusConnectionLostError,
    NoTokenError,
    SessionProtocolError,
    UnsupportedVersionError,
)

logger = logging.getLogger(__name__)


SUPPORTED_VERSION = (1, 2)


class SessionFactory:
    def __init__(
        self,
        config,
        authenticator,
        bus_service,
        protocol_encoder,
        protocol_decoder,
    ):
        self._config = config
        self._authenticator = authenticator
        self._bus_service = bus_service
        self._protocol_encoder = protocol_encoder
        self._protocol_decoder = protocol_decoder

    async def ws_handler(self, ws, path):
        remote_address = ws.request_headers.get('X-Forwarded-For', ws.remote_address)
        logger.info('websocket connection accepted from "%s"', remote_address)
        session = Session(
            self._config,
            self._authenticator,
            self._bus_service,
            self._protocol_encoder,
            self._protocol_decoder,
            ws,
            path,
        )
        try:
            await session.run()
        finally:
            logger.info('websocket session terminated %s', remote_address)


class Session:
    _CLOSE_CODE_NO_TOKEN_ID = 4001
    _CLOSE_CODE_AUTH_FAILED = 4002
    _CLOSE_CODE_AUTH_EXPIRED = 4003
    _CLOSE_CODE_PROTOCOL_ERROR = 4004

    def __init__(
        self,
        config,
        authenticator,
        bus_service,
        protocol_encoder,
        protocol_decoder,
        ws,
        path,
    ):
        self._ws_ping_interval = config['websocket']['ping_interval']
        self._authenticator = authenticator
        self._protocol_version = 1
        self._protocol_encoder = protocol_encoder
        self._protocol_decoder = protocol_decoder
        self._ws = ws
        self._path = path
        self._started = False
        self._bus_service: BusService = bus_service
        self._consumer: BusConsumer = None  # type: ignore[assignment]

    async def run(self):
        try:
            await self._run()
        except NoTokenError:
            logger.info('closing websocket connection: no token')
            await self._ws.close(self._CLOSE_CODE_NO_TOKEN_ID, 'no token')
        except AuthenticationExpiredError:
            logger.info('closing websocket connection: authentication expired')
            await self._ws.close(
                self._CLOSE_CODE_AUTH_EXPIRED, 'authentication expired'
            )
        except AuthenticationError as e:
            logger.info('closing websocket connection: authentication failed: %s', e)
            await self._ws.close(self._CLOSE_CODE_AUTH_FAILED, 'authentication failed')
        except SessionProtocolError as e:
            logger.info('closing websocket connection: session protocol error: %s', e)
            await self._ws.close(self._CLOSE_CODE_PROTOCOL_ERROR)
        except UnsupportedVersionError:
            logger.info('closing websocket connection: protocol version unknown')
            await self._ws.close(self._CLOSE_CODE_PROTOCOL_ERROR)
        except BusConnectionLostError:
            logger.info('closing websocket connection: bus connection lost')
            await self._ws.close(1011, 'bus connection lost')
        except BusConnectionError:
            logger.info('closing websocket connection: bus connection error')
            await self._ws.close(1011, 'bus connection error')
        except websockets.ConnectionClosed as e:
            # also raised when the ws_server is closed
            logger.info('websocket connection closed with code %s', e.code)
        except Exception:
            logger.exception('unexpected exception during websocket session run:')
            await self._ws.close(1011)

    async def _run(self):
        if not MasterTenantProxy.has_master_tenant():
            raise AuthenticationError('unable to determine master tenant')

        self._protocol_version = _extract_version_from_path(self._path)

        token_id = _extract_token_id(self._ws, self._path)
        token = await self._authenticator.get_token(token_id)

        async with await self._bus_service.create_consumer(token) as self._consumer:
            await self._ws.send(
                self._protocol_encoder.encode_init(version=self._protocol_version)
            )

            ping_task = asyncio.create_task(self._task_send_ping())
            receiver_task = asyncio.create_task(self._task_receive_command())
            transmitter_task = asyncio.create_task(self._task_transmit_event())
            auth_task = asyncio.create_task(self._task_authentification())

            tasks = [ping_task, receiver_task, auth_task, transmitter_task]

            # Wait for the first exception
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )

            # Then stop other tasks
            for task in pending:
                task.cancel()

            # Wait for all tasks to stop
            await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)

            # raise the first exception we got
            for task in done:
                task.result()

    async def _task_send_ping(self):
        while True:
            await asyncio.sleep(self._ws_ping_interval)
            logger.debug('sending websocket ping')
            try:
                await self._ws.ping()
            except websockets.ConnectionClosed:
                raise

    async def _task_receive_command(self):
        while True:
            data = await self._ws.recv()
            msg = self._protocol_decoder.decode(data)
            func_name = f'_do_ws_{msg.op}'
            func = getattr(self, func_name, None)
            if func is None:
                raise SessionProtocolError(f'unknown operation "{msg.op}"')
            await func(msg)

    async def _task_transmit_event(self):
        async for message in self._consumer:
            if not self._started:
                logger.debug(
                    'unable to push event to websocket as session hasn\'t started yet'
                )
                continue
            if self._protocol_version == 1:
                payload = message.raw
            else:
                payload = self._protocol_encoder.encode_event(message.content)
            await self._ws.send(payload)

    async def _task_authentification(self):
        await self._authenticator.run_check(self._consumer.get_token)

    async def _do_ws_subscribe(self, msg):
        event_name = msg.value
        logger.debug('subscribing to event "%s"', event_name)
        await self._consumer.bind(event_name)
        if not self._started or self._protocol_version == 2:
            await self._ws.send(self._protocol_encoder.encode_subscribe())

    async def _do_ws_start(self, msg):
        self._started = True
        await self._ws.send(self._protocol_encoder.encode_start())

    async def _do_ws_token(self, msg):
        token = await self._authenticator.get_token(msg.value)
        self._consumer.set_token(token)
        if not self._started or self._protocol_version == 2:
            await self._ws.send(self._protocol_encoder.encode_token())

    async def _do_ws_ping(self, msg):
        if self._protocol_version == 2:
            logger.debug('received client ping, sending pong')
            await self._ws.send(self._protocol_encoder.encode_pong(msg.value))
        else:
            logger.debug('received client ping, only supported in version 2')


def _extract_token_id(ws, path):
    token = _extract_token_id_from_path(path)
    if token:
        return token

    token = _extract_token_id_from_headers(ws.request_headers.raw_items())
    if token:
        return token
    raise NoTokenError()


def _extract_version_from_path(path):
    for name, value in parse_qsl(urlparse(path).query):
        if name == 'version':
            version = int(value)
            if version not in SUPPORTED_VERSION:
                raise UnsupportedVersionError()
            return version
    return 1


def _extract_token_id_from_path(path):
    for name, value in parse_qsl(urlparse(path).query):
        if name == 'token':
            return value
    return None


def _extract_token_id_from_headers(headers):
    for name, value in headers:
        if name.lower() == 'x-auth-token':
            return value
    return None
