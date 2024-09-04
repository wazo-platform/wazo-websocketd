# Copyright 2016-2024 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import collections
import json
import logging

from .exception import SessionProtocolError

logger = logging.getLogger(__name__)


class SessionProtocolEncoder:
    _CODE_SUCCESS = 0
    _CODE_FAILURE = 1

    def encode_init(self, version=2):
        return self._encode('init', {"version": version})

    def encode_subscribe(self):
        return self._encode('subscribe')

    def encode_start(self):
        return self._encode('start')

    def encode_token(self):
        return self._encode('token')

    def encode_event(self, event):
        return self._encode("event", event)

    def encode_pong(self, data):
        return self._encode("pong", data={"payload": data})

    def _encode(self, operation, data=None, code=_CODE_SUCCESS):
        return json.dumps({'op': operation, 'code': code, 'data': data})


class SessionProtocolDecoder:
    def decode(self, data):
        if not isinstance(data, str):
            raise SessionProtocolError(
                f'expected text frame: got data with type {type(data)}'
            )
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

        func_name = f'_decode_{operation}'
        func = getattr(self, func_name, self._decode)
        return func(operation, deserialized_data)

    def _decode(self, operation, deserialized_data):
        return _Message(operation, None)

    def _decode_token(self, operation, deserialized_data):
        return self._get("token", operation, deserialized_data)

    def _decode_subscribe(self, operation, deserialized_data):
        return self._get("event_name", operation, deserialized_data)

    def _decode_ping(self, operation, deserialized_data):
        return self._get("payload", operation, deserialized_data)

    @staticmethod
    def _get(attribute, operation, deserialized_data):
        if 'data' not in deserialized_data:
            raise SessionProtocolError('object is missing required "data" key')
        elif not isinstance(deserialized_data['data'], dict):
            raise SessionProtocolError('object "data" value is not an object')
        elif attribute not in deserialized_data['data']:
            raise SessionProtocolError(
                f'object "data" is missing required "{attribute}" key'
            )

        value = deserialized_data['data'][attribute]
        if not isinstance(value, str):
            raise SessionProtocolError(f'object data "{value}" value is not a string')

        return _Message(operation, value)


_Message = collections.namedtuple('_Message', ['op', 'value'])
