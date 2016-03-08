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
        self._started = False

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
    def recv_msg(self, timeout=_TIMEOUT):
        raw_msg = yield from self.recv(timeout)
        return json.loads(raw_msg)

    @asyncio.coroutine
    def _expect_msg(self, op, timeout=_TIMEOUT):
        msg = yield from self.recv_msg(timeout)
        if msg['op'] != op:
            raise AssertionError('expected op "{}": got op "{}"'.format(op, msg['op']))

    @asyncio.coroutine
    def _send_msg(self, msg):
        yield from self._websocket.send(json.dumps(msg))

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
        yield from self._expect_msg('init', timeout)

    @asyncio.coroutine
    def wait_for_nothing(self, timeout=_TIMEOUT):
        # Raise an exception if data is received during the next "timeout" seconds
        try:
            data = yield from self.recv(timeout)
        except WebSocketdTimeoutError:
            pass
        else:
            raise AssertionError('got unexpected data from websocket: {!r}'.format(data))

    @asyncio.coroutine
    def op_start(self):
        yield from self._send_msg({'op': 'start'})
        if self._started:
            return
        self._started = True
        yield from self._expect_msg('start')

    @asyncio.coroutine
    def op_subscribe(self, event_name):
        yield from self._send_msg({'op': 'subscribe', 'data': {'event_name': event_name}})
        if self._started:
            return
        yield from self._expect_msg('subscribe')

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
