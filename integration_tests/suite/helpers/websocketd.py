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
        self._version = 1
        self.timeout = self._DEFAULT_TIMEOUT

    async def close(self):
        if self._websocket:
            await self._websocket.close()
            self._websocket = None

    async def connect(self, token_id):
        url = 'wss://localhost:{port}/'.format(port=self._port)
        if token_id is not None:
            url = url + '?token={}'.format(token_id)

        self._websocket = await websockets.connect(
            url, loop=self._loop, ssl=self._SSL_CONTEXT
        )

    async def connect_and_wait_for_init(self, token_id):
        await self.connect(token_id)
        await self.wait_for_init()

    async def connect_and_wait_for_close(self, token_id, code=None):
        await self.connect(token_id)
        await self.wait_for_close(code)

    async def wait_for_close(self, code=None):
        # close code are defined in the "constants" module
        try:
            data = await self._recv()
        except websockets.ConnectionClosed as e:
            if code is not None and e.code != code:
                raise AssertionError(
                    'expected close code {}: got {}'.format(code, e.code)
                )
            return
        else:
            raise AssertionError('got unexpected data: {!r}'.format(data))

    async def wait_for_init(self):
        msg = await self._expect_msg('init')
        assert msg["data"]["version"] == 2

    async def wait_for_nothing(self):
        # Raise an exception if data is received during the next "self.timeout" seconds
        try:
            data = await self._recv()
        except WebSocketdTimeoutError:
            pass
        else:
            raise AssertionError(
                'got unexpected data from websocket: {!r}'.format(data)
            )

    async def recv_msg(self):
        raw_msg = await self._recv()
        return json.loads(raw_msg)

    async def _recv(self):
        timeout = self.timeout
        task = self._loop.create_task(self._websocket.recv())
        await asyncio.wait([task], loop=self._loop, timeout=timeout)
        if not task.done():
            task.cancel()
            raise WebSocketdTimeoutError(
                'recv() did not return in {} seconds'.format(timeout)
            )
        return task.result()

    async def _expect_msg(self, op):
        msg = await self.recv_msg()
        if msg['op'] != op:
            raise AssertionError('expected op "{}": got op "{}"'.format(op, msg['op']))
        return msg

    async def op_start(self, version=None):
        if version:
            self._version = 2
            await self._send_msg({'op': 'start', 'data': {'version': 2}})
        else:
            await self._send_msg({'op': 'start'})
        if self._started and self._version == 1:
            return
        await self._expect_msg('start')
        self._started = True

    async def op_token(self, token):
        await self._send_msg({'op': 'token', 'data': {'token': token}})
        if self._started and self._version == 1:
            return
        await self._expect_msg('token')

    async def op_subscribe(self, event_name):
        await self._send_msg({'op': 'subscribe', 'data': {'event_name': event_name}})
        if self._started and self._version == 1:
            return
        await self._expect_msg('subscribe')

    async def _send_msg(self, msg):
        await self._websocket.send(json.dumps(msg))
