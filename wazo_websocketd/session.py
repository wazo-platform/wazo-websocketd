# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from enum import IntEnum
from urllib.parse import parse_qsl, urlparse

import websockets

from .auth import MasterTenant
from .bus import BusService
from .consumers import UserBusSubscriber
from .exception import (
    AuthenticationError,
    AuthenticationExpiredError,
    AuthServerUnavailableError,
    BusConnectionError,
    BusConnectionLostError,
    NoTokenError,
    SessionProtocolError,
    SessionRevokedError,
    UnknownMasterTenantError,
    UnsupportedVersionError,
)

logger = logging.getLogger(__name__)


SUPPORTED_VERSION = (1, 2)


class CloseCode(IntEnum):
    GOING_AWAY = 1001
    INTERNAL_ERROR = 1011  # RFC 6455
    NO_TOKEN = 4001
    AUTH_FAILED = 4002
    AUTH_EXPIRED = 4003
    PROTOCOL_ERROR = 4004


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
            user_uuid, tenant_uuid = session.user_identity()
            logger.info(
                'websocket session terminated %s (user=%s tenant=%s)',
                remote_address,
                user_uuid,
                tenant_uuid,
            )


class Session:
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
        self._config = config
        self._ws_ping_interval = config['websocket']['ping_interval']
        self._authenticator = authenticator
        self._protocol_version = 1
        self._protocol_encoder = protocol_encoder
        self._protocol_decoder = protocol_decoder
        self._ws = ws
        self._path = path
        self._started = False
        self._bus_service: BusService = bus_service
        self._consumer: UserBusSubscriber = None  # type: ignore[assignment]
        self._user_uuid: str | None = None
        self._tenant_uuid: str | None = None

    def user_identity(self) -> tuple[str | None, str | None]:
        return self._user_uuid, self._tenant_uuid

    async def run(self):
        try:
            await self._run()
        except NoTokenError:
            logger.info(
                'closing websocket connection: no token (user=%s tenant=%s)',
                self._user_uuid,
                self._tenant_uuid,
            )
            await self._ws.close(CloseCode.NO_TOKEN, 'no token')
        except SessionRevokedError:
            logger.info(
                'closing websocket connection: session revoked (user=%s tenant=%s)',
                self._user_uuid,
                self._tenant_uuid,
            )
            await self._ws.close(CloseCode.AUTH_EXPIRED, 'session revoked')
        except AuthenticationExpiredError:
            logger.info(
                'closing websocket connection: authentication expired (user=%s tenant=%s)',
                self._user_uuid,
                self._tenant_uuid,
            )
            await self._ws.close(CloseCode.AUTH_EXPIRED, 'authentication expired')
        except AuthenticationError as e:
            logger.info(
                'closing websocket connection: authentication failed: %s (user=%s tenant=%s)',
                e,
                self._user_uuid,
                self._tenant_uuid,
            )
            await self._ws.close(CloseCode.AUTH_FAILED, 'authentication failed')
        except AuthServerUnavailableError:
            logger.info(
                'closing websocket connection: authentication server unavailable '
                '(user=%s tenant=%s)',
                self._user_uuid,
                self._tenant_uuid,
            )
            await self._ws.close(
                CloseCode.INTERNAL_ERROR, 'authentication server unavailable'
            )
        except UnknownMasterTenantError:
            logger.info(
                'closing websocket connection: worker is not ready, '
                'the master tenant is still unknown'
            )
            await self._ws.close(CloseCode.INTERNAL_ERROR, 'not ready')
        except SessionProtocolError as e:
            logger.info(
                'closing websocket connection: session protocol error: %s (user=%s tenant=%s)',
                e,
                self._user_uuid,
                self._tenant_uuid,
            )
            await self._ws.close(CloseCode.PROTOCOL_ERROR)
        except UnsupportedVersionError:
            logger.info(
                'closing websocket connection: protocol version unknown (user=%s tenant=%s)',
                self._user_uuid,
                self._tenant_uuid,
            )
            await self._ws.close(CloseCode.PROTOCOL_ERROR)
        except BusConnectionLostError:
            logger.info(
                'closing websocket connection: bus connection lost (user=%s tenant=%s)',
                self._user_uuid,
                self._tenant_uuid,
            )
            await self._ws.close(CloseCode.INTERNAL_ERROR, 'bus connection lost')
        except BusConnectionError:
            logger.info(
                'closing websocket connection: bus connection error (user=%s tenant=%s)',
                self._user_uuid,
                self._tenant_uuid,
            )
            await self._ws.close(CloseCode.INTERNAL_ERROR, 'bus connection error')
        except websockets.ConnectionClosed as e:
            # also raised when the ws_server is closed
            logger.info(
                'websocket connection closed with code %s (user=%s tenant=%s)',
                e.code,
                self._user_uuid,
                self._tenant_uuid,
            )
        except Exception:
            logger.exception(
                'unexpected exception during websocket session run: (user=%s tenant=%s)',
                self._user_uuid,
                self._tenant_uuid,
            )
            await self._ws.close(CloseCode.INTERNAL_ERROR)

    async def _run(self):
        if not MasterTenant.is_known():
            raise UnknownMasterTenantError()

        self._protocol_version = _extract_version_from_path(self._path)

        token_id = _extract_token_id(self._ws, self._path)
        token = await self._authenticator.get_token(token_id)
        self._user_uuid = token['metadata'].get('uuid')
        self._tenant_uuid = token['metadata'].get('tenant_uuid')

        async with UserBusSubscriber(
            self._bus_service.connection(), self._config, token
        ) as self._consumer:
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
            if error := _first_exception((*done, *pending)):
                raise error

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
        async for event in self._consumer:
            if not self._started:
                logger.debug(
                    'unable to push event to websocket as session hasn\'t started yet'
                )
                continue
            if self._protocol_version == 1:
                payload = event.raw
            else:
                payload = self._protocol_encoder.encode_event(event.content)
            await self._ws.send(payload)

    async def _task_authentification(self):
        await self._authenticator.run_check(self._consumer.get_token)

    async def _do_ws_subscribe(self, msg):
        event_name = msg.value
        logger.debug('subscribing to event "%s"', event_name)
        await self._consumer.subscribe(event_name)
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


def _first_exception(tasks: Iterable[asyncio.Task]) -> BaseException | None:
    """Read every task's outcome, returning the first exception raised.

    Siblings that failed in the same loop iteration must be read too, or
    asyncio reports them as never retrieved once they are collected.
    """
    first = None
    for task in tasks:
        if task.cancelled():
            continue
        if (error := task.exception()) is not None and first is None:
            first = error
    return first


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
