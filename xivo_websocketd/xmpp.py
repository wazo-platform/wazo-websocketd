# Copyright 2017 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import logging
import os

from slixmpp import ClientXMPP
from .exception import MongooseIMError

logger = logging.getLogger(__name__)


class ClientXMPPWrapper():

    def __init__(self, host, xmpp_port, **kwargs):
        self._host = host
        self._port = xmpp_port
        self._handlers = []
        self._client = None
        self.username = None

    @asyncio.coroutine
    def connect(self, username, password, loop):
        if not username or not password:
            logger.warning('cannot create XMPP session: missing username/password')
        self.username = username
        jid = '{}@localhost'.format(username)
        self._client = _ClientXMPP(jid, password)
        self.connection_error_handler(self._close_callback)
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

    @asyncio.coroutine
    def _close_callback(self, event):
        self.close()

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
        if presence == 'disconnected':
            self.close()
            return
        self._client.send_presence(pstatus=presence)

    @asyncio.coroutine
    def resource(self, loop):
        if not self._client:
            return
        # resource is populated after the connection
        while not self._client.boundjid.resource:
            yield from asyncio.sleep(0.1, loop=loop)
        return self._client.boundjid.resource


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


class MongooseIMClient(object):

    domain = 'localhost'
    entrypoint = '/usr/bin/mongooseimctl'

    def __init__(self):
        # When running "mongooseimctl", erlang creates a ".erlang.cookie" to set
        # the cluster on which the command must run. In our case, we don't care
        # of this file, but we must set the variable HOME to use "mongooseimctl"
        os.environ['HOME'] = os.environ.get('HOME') or '/var/lib/mongooseim'

    @asyncio.coroutine
    def get_presence(self, username):
        cmd = [self.entrypoint, 'get_presence', username, self.domain]
        process = yield from self._stream_subprocess(cmd)
        presence = yield from self._process_stdout_get_presence(process.stdout)
        if process.returncode != 0:
            raise MongooseIMError('getting presence of user "{}@{}" failed'.format(username, self.domain))
        return presence

    @asyncio.coroutine
    def set_presence(self, username, resource, presence):
        if presence == 'disconnected':
            reason = ""
            cmd = [self.entrypoint, 'kick_session', username, self.domain, resource, reason]
            logger.debug('disconnecting "{}@{}/{}"'.format(username, self.domain, resource))
        else:
            type_, show, priority = '', '', ''
            status = presence
            cmd = [self.entrypoint, 'set_presence', username, self.domain, resource, type_, show, status, priority]
            logger.debug('updating "{}@{}/{}" with presence "{}"'.format(username, self.domain, resource, presence))
        process = yield from self._stream_subprocess(cmd)
        if process.returncode != 0:
            raise MongooseIMError('setting "{}@{}/{}" presence to "{}" failed'.format(username, self.domain,
                                                                                      resource, presence))

    @asyncio.coroutine
    def get_user_resources(self, username):
        cmd = [self.entrypoint, 'user_resources', username, self.domain]
        process = yield from self._stream_subprocess(cmd)
        resources = yield from self._process_stdout_user_resources(process.stdout)
        logger.debug('user "{}" has the following connected resources: {}'.format(username, resources))
        if process.returncode != 0:
            raise MongooseIMError('getting "{}@{}" resource failed'.format(username, self.domain))
        return resources

    @asyncio.coroutine
    def _process_stdout_user_resources(self, stream):
        resources = []
        while True:
            line = yield from stream.readline()
            if not line:
                break
            resources.append(line.decode('utf-8').strip())
        return resources

    @asyncio.coroutine
    def _process_stdout_get_presence(self, stream):
        first_line = yield from stream.readline()
        result = first_line.decode('utf-8').split()  # [jid, show, status]
        if len(result) > 2:
            presence = result[2]  # status
        elif len(result) == 2:
            presence = result[1]  # show
        else:
            presence = 'disconnected'

        if presence == 'unavailable':
            presence = 'disconnected'
        return presence

    @asyncio.coroutine
    def _stream_subprocess(self, cmd):
        process = yield from asyncio.create_subprocess_exec(*cmd,
                                                            stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.PIPE)
        yield from process.wait()
        return process
