# Copyright 2016-2017 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import logging

from urllib.parse import urlparse, parse_qsl

import websockets

from xivo_websocketd.exception import AuthenticationError,\
    NoTokenError, SessionProtocolError, BusConnectionLostError,\
    AuthenticationExpiredError, BusConnectionError
from xivo_websocketd.multiplexer import Multiplexer

logger = logging.getLogger(__name__)


class SessionFactory(object):

    def __init__(self, config, loop, authenticator, bus_event_service, protocol_encoder, protocol_decoder):
        self._config = config
        self._loop = loop
        self._authenticator = authenticator
        self._bus_event_service = bus_event_service
        self._protocol_encoder = protocol_encoder
        self._protocol_decoder = protocol_decoder

    @asyncio.coroutine
    def ws_handler(self, ws, path):
        remote_address = ws.request_headers.get('X-Forwarded-For', ws.remote_address)
        logger.info('websocket connection accepted %s', remote_address)
        session = Session(self._config, self._loop, self._authenticator, self._bus_event_service,
                          self._protocol_encoder, self._protocol_decoder, ws, path)
        try:
            yield from session.run()
        finally:
            logger.info('websocket session terminated %s', remote_address)


class Session(object):

    _CLOSE_CODE_NO_TOKEN_ID = 4001
    _CLOSE_CODE_AUTH_FAILED = 4002
    _CLOSE_CODE_AUTH_EXPIRED = 4003
    _CLOSE_CODE_PROTOCOL_ERROR = 4004

    def __init__(self, config, loop, authenticator, bus_event_service, protocol_encoder, protocol_decoder, ws, path):
        self._ws_ping_interval = config['websocket']['ping_interval']
        self._loop = loop
        self._authenticator = authenticator
        self._bus_event_service = bus_event_service
        self._protocol_encoder = protocol_encoder
        self._protocol_decoder = protocol_decoder
        self._ws = ws
        self._path = path
        self._multiplexer = Multiplexer(self._loop)
        self._started = False

    @asyncio.coroutine
    def run(self):
        try:
            yield from self._run()
        except NoTokenError:
            logger.info('closing websocket connection: no token')
            yield from self._ws.close(self._CLOSE_CODE_NO_TOKEN_ID, 'no token')
        except AuthenticationExpiredError as e:
            logger.info('closing websocket connection: authentication expired')
            yield from self._ws.close(self._CLOSE_CODE_AUTH_EXPIRED, 'authentication expired')
        except AuthenticationError as e:
            logger.info('closing websocket connection: authentication failed: %s', e)
            yield from self._ws.close(self._CLOSE_CODE_AUTH_FAILED, 'authentication failed')
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

    @asyncio.coroutine
    def _run(self):
        token_id = _extract_token_id(self._ws, self._path)
        token = yield from self._authenticator.get_token(token_id)
        self._bus_event_consumer = yield from self._bus_event_service.new_event_consumer(token)

        try:
            yield from self._ws.send(self._protocol_encoder.encode_init())

            self._multiplexer.call_later(self._ws_ping_interval, self._send_ping)
            self._multiplexer.call_when_done(self._authenticator.run_check(token), self._on_authenticator_check)
            self._multiplexer.call_when_done(self._ws.recv(), self._on_ws_recv)
            self._multiplexer.call_when_done(self._bus_event_consumer.get(), self._on_bus_event)
            yield from self._multiplexer.run()
        finally:
            self._bus_event_consumer.close()
            yield from self._multiplexer.close()

    @asyncio.coroutine
    def _send_ping(self):
        logger.debug('sending websocket ping')
        yield from self._ws.ping()
        self._multiplexer.call_later(self._ws_ping_interval, self._send_ping)

    def _on_authenticator_check(self, future):
        # just call future.result and expect the coroutine to raise an exception
        future.result()
        raise AssertionError('should never be reached')

    @asyncio.coroutine
    def _on_ws_recv(self, future):
        data = future.result()
        msg = self._protocol_decoder.decode(data)
        func_name = '_do_ws_{}'.format(msg.op)
        func = getattr(self, func_name, None)
        if func is None:
            raise SessionProtocolError('unknown operation "{}"'.format(msg.op))
        yield from func(msg)
        self._multiplexer.call_when_done(self._ws.recv(), self._on_ws_recv)

    @asyncio.coroutine
    def _do_ws_subscribe(self, msg):
        logger.debug('subscribing to event "%s"', msg.event_name)
        self._bus_event_consumer.subscribe_to_event(msg.event_name)
        if not self._started:
            yield from self._ws.send(self._protocol_encoder.encode_subscribe())

    @asyncio.coroutine
    def _do_ws_start(self, msg):
        if self._started:
            return
        self._started = True
        yield from self._ws.send(self._protocol_encoder.encode_start())

    @asyncio.coroutine
    def _on_bus_event(self, future):
        bus_event = future.result()
        if self._started:
            yield from self._ws.send(bus_event.msg_body)
        else:
            logger.debug('not sending bus event to websocket: session not started')
        self._multiplexer.call_when_done(self._bus_event_consumer.get(), self._on_bus_event)


def _extract_token_id(ws, path):
    token = _extract_token_id_from_path(path)
    if token:
        return token

    token = _extract_token_id_from_headers(ws.raw_request_headers)
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
