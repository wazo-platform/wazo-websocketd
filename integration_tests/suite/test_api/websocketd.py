# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import asyncio
import json
import ssl

import websockets


class WebSocketdTimeoutError(Exception):    
    pass


class WebSocketdClient(object):

    _TIMEOUT = 5
    _SSL_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    _SSL_CONTEXT.verify_mode = ssl.CERT_NONE

    def __init__(self, loop):
        self._loop = loop
        self._websocket = None

    @asyncio.coroutine
    def close(self):
        if self._websocket:
            yield from self._websocket.close()
            self._websocket = None

    @asyncio.coroutine
    def connect(self, token_id):
        url = 'wss://localhost:9502/'
        if token_id is not None:
            url = url + '?token={}'.format(token_id)

        self._websocket = yield from websockets.connect(url, loop=self._loop, ssl=self._SSL_CONTEXT)

    @asyncio.coroutine
    def connect_and_wait_for_init(self, token_id):
        yield from self.connect(token_id)
        try:
            yield from self.wait_for_init()
        except Exception:
            yield from self.close()
            raise

    @asyncio.coroutine
    def recv(self, timeout=_TIMEOUT):
        task = self._loop.create_task(self._websocket.recv())
        yield from asyncio.wait([task], loop=self._loop, timeout=timeout)
        if not task.done():
            task.cancel()
            raise WebSocketdTimeoutError('recv() did not return in {} seconds'.format(timeout))
        return task.result()

    @asyncio.coroutine
    def wait_for_close(self, code=None, timeout=_TIMEOUT):
        # close code are defined in the "constants" module
        try:
            data = yield from self.recv(timeout)
        except websockets.ConnectionClosed as e:
            if code is not None and e.code != code:
                raise AssertionError('expected close code {}: got {}'.format(code, e.code))
            return
        else:
            raise AssertionError('got unexpected data: {!r}'.format(data))

    @asyncio.coroutine
    def wait_for_init(self, timeout=_TIMEOUT):
        try:
            data = yield from self.recv(timeout)
        except WebSocketdTimeoutError:
            return
        else:
            msg = json.loads(data)
            if msg['op'] != 'init':
                raise AssertionError('expected op "init": got op "{}"'.format(msg['op']))

    @asyncio.coroutine
    def op_start(self):
        data = json.dumps({'op': 'start'})
        yield from self._websocket.send(data)
        # XXX if the connection is already started, we won't receive a reply
        response_data = yield from self.recv()
        response = json.loads(response_data)
        if response['op'] != 'start':
            raise AssertionError('expected op "start": got op "{}"'.format(response['op']))

    @asyncio.coroutine
    def test_connect_success(self, token_id):
        yield from self.connect(token_id)
        try:
            yield from self.wait_for_init()
        finally:
            yield from self.close()

    @asyncio.coroutine
    def test_connect_failure(self, token_id, code=None):
        yield from self.connect(token_id)
        try:
            yield from self.wait_for_close(code)
        finally:
            yield from self.close()
