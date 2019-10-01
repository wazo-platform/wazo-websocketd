# Copyright 2016-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import json
import ssl

import websockets


class WebSocketdTimeoutError(Exception):
    pass


class WebSocketdClient(object):

    _DEFAULT_TIMEOUT = 5
    _SSL_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
    _SSL_CONTEXT.verify_mode = ssl.CERT_NONE

    def __init__(self, loop, port):
        self._loop = loop
        self._port = port
        self._websocket = None
        self._started = False
        self.timeout = self._DEFAULT_TIMEOUT

    @asyncio.coroutine
    def close(self):
        if self._websocket:
            yield from self._websocket.close()
            self._websocket = None

    @asyncio.coroutine
    def connect(self, token_id):
        url = 'wss://localhost:{port}/'.format(port=self._port)
        if token_id is not None:
            url = url + '?token={}'.format(token_id)

        self._websocket = yield from websockets.connect(
            url, loop=self._loop, ssl=self._SSL_CONTEXT
        )

    @asyncio.coroutine
    def connect_and_wait_for_init(self, token_id):
        yield from self.connect(token_id)
        yield from self.wait_for_init()

    @asyncio.coroutine
    def connect_and_wait_for_close(self, token_id, code=None):
        yield from self.connect(token_id)
        yield from self.wait_for_close(code)

    @asyncio.coroutine
    def wait_for_close(self, code=None):
        # close code are defined in the "constants" module
        try:
            data = yield from self._recv()
        except websockets.ConnectionClosed as e:
            if code is not None and e.code != code:
                raise AssertionError(
                    'expected close code {}: got {}'.format(code, e.code)
                )
            return
        else:
            raise AssertionError('got unexpected data: {!r}'.format(data))

    @asyncio.coroutine
    def wait_for_init(self):
        yield from self._expect_msg('init')

    @asyncio.coroutine
    def wait_for_nothing(self):
        # Raise an exception if data is received during the next "self.timeout" seconds
        try:
            data = yield from self._recv()
        except WebSocketdTimeoutError:
            pass
        else:
            raise AssertionError(
                'got unexpected data from websocket: {!r}'.format(data)
            )

    @asyncio.coroutine
    def recv_msg(self):
        raw_msg = yield from self._recv()
        return json.loads(raw_msg)

    @asyncio.coroutine
    def _recv(self):
        timeout = self.timeout
        task = self._loop.create_task(self._websocket.recv())
        yield from asyncio.wait([task], loop=self._loop, timeout=timeout)
        if not task.done():
            task.cancel()
            raise WebSocketdTimeoutError(
                'recv() did not return in {} seconds'.format(timeout)
            )
        return task.result()

    @asyncio.coroutine
    def _expect_msg(self, op):
        msg = yield from self.recv_msg()
        if msg['op'] != op:
            raise AssertionError('expected op "{}": got op "{}"'.format(op, msg['op']))
        return msg

    @asyncio.coroutine
    def op_start(self):
        yield from self._send_msg({'op': 'start'})
        if self._started:
            return
        self._started = True
        yield from self._expect_msg('start')

    @asyncio.coroutine
    def op_subscribe(self, event_name):
        yield from self._send_msg(
            {'op': 'subscribe', 'data': {'event_name': event_name}}
        )
        if self._started:
            return
        yield from self._expect_msg('subscribe')

    @asyncio.coroutine
    def _send_msg(self, msg):
        yield from self._websocket.send(json.dumps(msg))
