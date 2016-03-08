# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import collections
import json
import logging

from xivo_websocketd.exception import SessionProtocolError

logger = logging.getLogger(__name__)


class SessionProtocolFactory(object):

    def __init__(self):
        self._encoder = _SessionProtocolEncoder()
        self._decoder = _SessionProtocolDecoder()

    def new_session_protocol(self, bus_service, ws):
        return _SessionProtocol(bus_service, ws, self._encoder, self._decoder)


class _SessionProtocol(object):

    def __init__(self, bus_service, ws, encoder, decoder):
        self._bus_service = bus_service
        self._ws = ws
        self._encoder = encoder
        self._decoder = decoder
        self._started = False

    @asyncio.coroutine
    def on_init_completed(self):
        yield from self._ws.send(self._encoder.encode_init())

    @asyncio.coroutine
    def on_bus_msg_received(self, msg):
        if self._started:
            yield from self._ws.send(msg.body.decode('utf-8'))
        else:
            logger.debug('not sending bus msg to websocket: session not started')

    @asyncio.coroutine
    def on_ws_data_received(self, data):
        msg = self._decoder.decode(data)
        func_name = '_do_ws_{}'.format(msg.op)
        func = getattr(self, func_name, None)
        if func is None:
            raise SessionProtocolError('unknown operation "{}"'.format(msg.op))
        yield from func(msg)

    @asyncio.coroutine
    def _do_ws_start(self, msg):
        if self._started:
            return
        self._started = True
        yield from self._ws.send(self._encoder.encode_start())


class _SessionProtocolEncoder(object):

    _CODE_OK = 0
    _MSG_OK = ''

    def encode_init(self):
        return self._encode('init')

    def encode_start(self):
        return self._encode('start')

    def _encode(self, operation, code=_CODE_OK, msg=_MSG_OK):
        return json.dumps({'op': operation, 'code': code, 'msg': msg})


class _SessionProtocolDecoder(object):

    def decode(self, data):
        if not isinstance(data, str):
            raise SessionProtocolError('expected text frame: got data with type {}'.format(type(data)))
        try:
            deserialized_data = json.loads(data)
        except ValueError:
            raise SessionProtocolError('not a valid json document')
        if not isinstance(deserialized_data, dict):
            raise SessionProtocolError('json document root is not an object')
        if 'op' not in deserialized_data:
            raise SessionProtocolError('object is missing required "op" key')
        operation = deserialized_data['op']
        if not isinstance(operation, str):
            raise SessionProtocolError('object "op" value is not a string')

        func_name = '_decode_{}'.format(operation)
        func = getattr(self, func_name, self._decode)
        return func(operation, deserialized_data)

    def _decode(self, operation, deserialized_data):
        return _Message(operation)


_Message = collections.namedtuple('_Message', ['op'])
