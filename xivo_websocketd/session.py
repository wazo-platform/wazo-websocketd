# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import logging

from urllib.parse import urlparse, parse_qsl

import websockets

from xivo_websocketd.exception import AuthenticationError,\
    NoTokenError, SessionProtocolError, BusConnectionLostError,\
    AuthenticationExpiredError
from xivo_websocketd.multiplexer import Multiplexer
from xivo_websocketd.protocol import SessionProtocolFactory

logger = logging.getLogger(__name__)


class SessionFactory(object):

    def __init__(self, config, loop, authenticator, bus_service_factory):
        self._config = config
        self._loop = loop
        self._authenticator = authenticator
        self._bus_service_factory = bus_service_factory
        self._session_protocol_factory = SessionProtocolFactory()
        self._sessions = []

    def on_bus_connection_lost(self):
        for session in self._sessions:
            session.on_bus_connection_lost()

    @asyncio.coroutine
    def ws_handler(self, ws, path):
        logger.info('websocket connection accepted (%s)', ws.remote_address)
        bus_service = self._bus_service_factory.new_bus_service()
        session_protocol = self._session_protocol_factory.new_session_protocol(bus_service, ws)
        session = Session(self._config, self._loop, self._authenticator, bus_service,
                          session_protocol, ws, path)
        self._sessions.append(session)
        try:
            yield from session.run()
        finally:
            self._sessions.remove(session)
            logger.info('websocket connection terminated (%s)', ws.remote_address)


class Session(object):

    _CLOSE_CODE_NO_TOKEN_ID = 4001
    _CLOSE_CODE_AUTH_FAILED = 4002
    _CLOSE_CODE_AUTH_EXPIRED = 4003
    _CLOSE_CODE_PROTOCOL_ERROR = 4004

    def __init__(self, config, loop, authenticator, bus_service, session_protocol, ws, path):
        self._ws_ping_interval = config['ws_ping_interval']
        self._loop = loop
        self._authenticator = authenticator
        self._bus_service = bus_service
        self._bus_service.set_callback(self.on_bus_msg_received)
        self._session_protocol = session_protocol
        self._ws = ws
        self._path = path
        self._multiplexer = Multiplexer(self._loop)
        self._bus_connection_lost = False

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
        except websockets.ConnectionClosed as e:
            # also raised when the ws_server is closed
            logger.info('websocket connection closed (%s)', e.code)
        except Exception:
            logger.exception('unexpected exception during websocket session run:')
            yield from self._ws.close(1011)

    @asyncio.coroutine
    def _run(self):
        token_id = _extract_token_id(self._ws, self._path)
        token = yield from self._authenticator.get_token(token_id)
        yield from self._bus_service.connect()
        try:
            yield from self._session_protocol.on_init_completed()

            self._multiplexer.call_later(self._ws_ping_interval, self._send_ping)
            self._multiplexer.call_when_done(self._authenticator.run_check(token), self._on_authenticator_check)
            self._multiplexer.call_when_done(self._ws.recv(), self._on_ws_recv)
            yield from self._multiplexer.run()
        finally:
            # only close bus_service if connection has not been lost, otherwise it hangs the coroutine
            # XXX this logic of "closing if no exception happened" could be moved inside the bus module
            if not self._bus_connection_lost:
                yield from self._bus_service.close()
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
        yield from self._session_protocol.on_ws_data_received(data)
        self._multiplexer.call_when_done(self._ws.recv(), self._on_ws_recv)

    def on_bus_connection_lost(self):
        # This might be called multiple time, since when having multiple
        # connections with asynqp, it's not possible to distinguish which
        # one was closed
        if self._bus_connection_lost:
            return

        self._bus_connection_lost = True
        self._multiplexer.raise_exception(BusConnectionLostError())

    def on_bus_msg_received(self, msg):
        self._multiplexer.call_soon(self._session_protocol.on_bus_msg_received, msg)


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
