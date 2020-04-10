# Copyright 2016-2020 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import json

import requests

from requests.packages.urllib3 import disable_warnings


class AuthServer(object):
    def __init__(self, loop, port):
        self._loop = loop
        self._base_url = 'http://localhost:{port}'.format(port=port)
        self._session = requests.Session()
        disable_warnings()

    def put_token(self, token_id, user_uuid='123-456', acls=['websocketd']):
        token = {'token': token_id, 'acls': acls, 'metadata': {'uuid': user_uuid}}
        url = '{}/_set_token'.format(self._base_url)
        headers = {'Content-Type': 'application/json'}
        r = self._session.post(url, headers=headers, data=json.dumps(token))
        if r.status_code != 204:
            r.raise_for_status()

    def remove_token(self, token_id):
        url = '{}/_remove_token/{}'.format(self._base_url, token_id)
        r = self._session.delete(url)
        if r.status_code != 204:
            r.raise_for_status()
