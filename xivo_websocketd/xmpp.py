# Copyright 2017 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import logging

from slixmpp import ClientXMPP

logger = logging.getLogger(__name__)


class ClientXMPPWrapper():

    def __init__(self, host, xmpp_port, **kwargs):
        self._host = host
        self._port = xmpp_port
        self._handlers = []
        self._client = None

    @asyncio.coroutine
    def connect(self, username, password, loop):
        if not username or not password:
            logger.warning('cannot create XMPP session: missing username/password')
        jid = '{}@localhost'.format(username)
        self._client = _ClientXMPP(jid, password)
        for handler in self._handlers:
            self._client.add_event_handler(*handler)
        self._client.connect((self._host, self._port))
        yield from self.wait_until_connected(loop)

    @asyncio.coroutine
    def wait_until_connected(self, loop):
        # If and error occurs during the connection, then connection_error_handler
        # is executed. So we don't care to set a timeout or reattempt, we just
        # want to block the coroutine from loop object
        while not self._client.is_connected():
            yield from asyncio.sleep(0.5, loop=loop)

    def close(self):
        if self._client is None:
            return
        self._client.disconnect()
        self._stop_connect_coroutine()

    def _stop_connect_coroutine(self):
        # slixmpp always start a new coroutine on OSError to retry to reconnect.
        # But if xmpp server is down and client try to connect, then there will
        # be a bunch of coroutine running until server will be up. To avoid this,
        # we simply trigger another error than OSError (in this case it's
        # socket.gaierror)
        self._client.address = ('Invalid address', 999999)

    def connection_error_handler(self, func):
        self._handlers.append(['session_end', func])
        self._handlers.append(['connection_failed', func])

    def send_presence(self, presence):
        self._client.send_presence(pstatus=presence)


class _ClientXMPP(ClientXMPP):

    # FIXME slixmpp resolver try to get DNS Record on each connection,
    #       we should bypass this check, since we don't use DNS

    def __init__(self, jid, password):
        super().__init__(jid, password, sasl_mech='PLAIN')
        self.add_event_handler("ssl_invalid_cert", self.discard)
        self.add_event_handler("session_start", self.start)

    @asyncio.coroutine
    def discard(self, event):
        pass

    @asyncio.coroutine
    def start(self, event):
        self.send_presence()
