# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import collections
import json
import logging

from xivo_websocketd.exception import SessionProtocolError

logger = logging.getLogger(__name__)


class SessionProtocolEncoder(object):

    _CODE_OK = 0
    _MSG_OK = ''

    def encode_init(self):
        return self._encode('init')

    def encode_start(self):
        return self._encode('start')

    def _encode(self, operation, code=_CODE_OK, msg=_MSG_OK):
        return json.dumps({'op': operation, 'code': code, 'msg': msg})


class SessionProtocolDecoder(object):

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
