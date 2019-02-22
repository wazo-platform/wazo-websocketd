# Copyright 2017 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import requests


class MongooseIMClient(object):

    def __init__(self, port):
        self._port = port

    def sessions(self):
        url = u'http://localhost:{port}/api/sessions/localhost'.format(port=self._port)
        response = requests.get(url)
        if response.status_code != 200:
            raise AssertionError('xmpp server unreachable')
        return response.json()
