# Copyright 2016 by Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import ssl

import websockets


class WebSocketdTimeoutError(Exception):    
    pass


class WebSocketdClient(object):

    _TIMEOUT = 5
    _SSL_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    _SSL_CONTEXT.verify_mode = ssl.CERT_NONE

    def __init__(self, event_loop):
        self._loop = event_loop
        self._websocket = None

    @asyncio.coroutine
    def connect(self, token_id):
        if token_id is not None:
            headers = {'X-Auth-Token': token_id}
        else:
            headers = None

        self._websocket = yield from websockets.connect('wss://localhost:9502',
                                                        loop=self._loop,
                                                        extra_headers=headers,
                                                        ssl=self._SSL_CONTEXT)

    @asyncio.coroutine
    def close(self):
        if self._websocket:
            yield from self._websocket.close()
            self._websocket = None

    @asyncio.coroutine
    def recv(self, timeout=_TIMEOUT):
        task = self._loop.create_task(self._websocket.recv())
        yield from asyncio.wait([task], loop=self._loop, timeout=timeout)
        if not task.done():
            raise WebSocketdTimeoutError('recv() did not return in {} seconds'.format(timeout))
        return task.result()

    @asyncio.coroutine
    def wait_for_close(self, timeout=_TIMEOUT):
        try:
            data = yield from self.recv(timeout)
        except websockets.ConnectionClosed:
            return
        else:
            raise AssertionError('got unexpected data: {!r}'.format(data))

    @asyncio.coroutine
    def wait_for_nothing(self, timeout=_TIMEOUT):
        try:
            data = yield from self.recv(timeout)
        except WebSocketdTimeoutError:
            return
        else:
            raise AssertionError('got unexpected data: {!r}'.format(data))
