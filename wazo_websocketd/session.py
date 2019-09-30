# Copyright 2016-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import logging

from urllib.parse import urlparse, parse_qsl

import websockets

from .acl import ACLCheck
from .exception import (
    AuthenticationError,
    AuthenticationExpiredError,
    BusConnectionError,
    BusConnectionLostError,
    NoTokenError,
    SessionProtocolError,
)

logger = logging.getLogger(__name__)


class SessionFactory(object):
    def __init__(
        self,
        config,
        authenticator,
        bus_event_service,
        protocol_encoder,
        protocol_decoder,
    ):
        self._config = config
        self._authenticator = authenticator
        self._bus_event_service = bus_event_service
        self._protocol_encoder = protocol_encoder
        self._protocol_decoder = protocol_decoder

    @asyncio.coroutine
    def ws_handler(self, ws, path):
        remote_address = ws.request_headers.get('X-Forwarded-For', ws.remote_address)
        logger.info('websocket connection accepted from "%s"', remote_address)
        session = Session(
            self._config,
            self._authenticator,
            self._bus_event_service,
            self._protocol_encoder,
            self._protocol_decoder,
            ws,
            path,
        )
        try:
            yield from session.run()
        finally:
            logger.info('websocket session terminated %s', remote_address)


class EventTransmitter(object):
    def __init__(self):
        self._queue = asyncio.Queue()
        self._event_names = set()
        self._all_events = False

    def set_token(self, token):
        self._token = token
        self._acl_check = ACLCheck(token['metadata']['uuid'], token['acls'])

    def get_token(self):
        return self._token

    def subscribe_to_event(self, event_name):
        if event_name == '*':
            self._all_events = True
        else:
            self._event_names.add(event_name)

    @asyncio.coroutine
    def get(self):
        # Raise a BusConnectionLostError when the connection to the bus is lost.
        bus_event = yield from self._queue.get()
        if bus_event is None:
            raise BusConnectionLostError()
        return bus_event

    def put(self, bus_event):
        if self._all_events or bus_event.name in self._event_names:
            if self._acl_check.matches_required_acl(bus_event.acl):
                self._queue.put_nowait(bus_event)

    def put_connection_lost(self):
        self._queue.put_nowait(None)


class Session(object):

    _CLOSE_CODE_NO_TOKEN_ID = 4001
    _CLOSE_CODE_AUTH_FAILED = 4002
    _CLOSE_CODE_AUTH_EXPIRED = 4003
    _CLOSE_CODE_PROTOCOL_ERROR = 4004

    def __init__(
        self,
        config,
        authenticator,
        bus_event_service,
        protocol_encoder,
        protocol_decoder,
        ws,
        path,
    ):
        self._ws_ping_interval = config['websocket']['ping_interval']
        self._authenticator = authenticator
        self._bus_event_service = bus_event_service
        self._protocol_version = 1
        self._protocol_encoder = protocol_encoder
        self._protocol_decoder = protocol_decoder
        self._ws = ws
        self._path = path
        self._started = False
        self._event_transmiter = None

    @asyncio.coroutine
    def run(self):
        try:
            yield from self._run()
        except NoTokenError:
            logger.info('closing websocket connection: no token')
            yield from self._ws.close(self._CLOSE_CODE_NO_TOKEN_ID, 'no token')
        except AuthenticationExpiredError:
            logger.info('closing websocket connection: authentication expired')
            yield from self._ws.close(
                self._CLOSE_CODE_AUTH_EXPIRED, 'authentication expired'
            )
        except AuthenticationError as e:
            logger.info('closing websocket connection: authentication failed: %s', e)
            yield from self._ws.close(
                self._CLOSE_CODE_AUTH_FAILED, 'authentication failed'
            )
        except SessionProtocolError as e:
            logger.info('closing websocket connection: session protocol error: %s', e)
            yield from self._ws.close(self._CLOSE_CODE_PROTOCOL_ERROR)
        except BusConnectionLostError:
            logger.info('closing websocket connection: bus connection lost')
            yield from self._ws.close(1011, 'bus connection lost')
        except BusConnectionError:
            logger.info('closing websocket connection: bus connection error')
            yield from self._ws.close(1011, 'bus connection error')
        except websockets.ConnectionClosed as e:
            # also raised when the ws_server is closed
            logger.info('websocket connection closed with code %s', e.code)
        except Exception:
            logger.exception('unexpected exception during websocket session run:')
            yield from self._ws.close(1011)

    async def _run(self):
        token_id = _extract_token_id(self._ws, self._path)
        _token = await self._authenticator.get_token(token_id)

        self._event_transmiter = EventTransmitter()
        self._event_transmiter.set_token(_token)
        await self._bus_event_service.register_event_consumer(self._event_transmiter)

        try:
            await self._ws.send(self._protocol_encoder.encode_init())

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

        finally:
            self._bus_event_service.unregister_event_consumer(self._event_transmiter)

    async def _task_send_ping(self):
        while True:
            await asyncio.sleep(self._ws_ping_interval)
            logger.debug('sending websocket ping')
            await self._ws.ping()

    async def _task_receive_command(self):
        while True:
            data = await self._ws.recv()
            msg = self._protocol_decoder.decode(data)
            func_name = '_do_ws_{}'.format(msg.op)
            func = getattr(self, func_name, None)
            if func is None:
                raise SessionProtocolError('unknown operation "{}"'.format(msg.op))
            await func(msg)

    async def _task_transmit_event(self):
        while True:
            bus_event = await self._event_transmiter.get()
            if self._started:
                if self._protocol_version == 1:
                    await self._ws.send(bus_event.msg_body)
                else:
                    await self._ws.send(
                        self._protocol_encoder.encode_event(bus_event.msg_body)
                    )
            else:
                logger.debug('not sending bus event to websocket: session not started')

    async def _task_authentification(self):
        await self._authenticator.run_check(self._event_transmiter.get_token)

    @asyncio.coroutine
    def _do_ws_subscribe(self, msg):
        logger.debug('subscribing to event "%s"', msg.value)
        self._event_transmiter.subscribe_to_event(msg.value)
        if not self._started or self._protocol_version == 2:
            yield from self._ws.send(self._protocol_encoder.encode_subscribe())

    @asyncio.coroutine
    def _do_ws_start(self, msg):
        self._started = True
        self._protocol_version = msg.value
        yield from self._ws.send(self._protocol_encoder.encode_start())

    @asyncio.coroutine
    def _do_ws_token(self, msg):
        token = self._authenticator.get_token(msg.value)
        self._event_transmiter.set_token(token)
        if not self._started or self._protocol_version == 2:
            yield from self._ws.send(self._protocol_encoder.encode_start())


def _extract_token_id(ws, path):
    token = _extract_token_id_from_path(path)
    if token:
        return token

    token = _extract_token_id_from_headers(ws.request_headers.raw_items())
    if token:
        return token
    raise NoTokenError()


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
