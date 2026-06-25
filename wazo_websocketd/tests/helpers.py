# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import socket


def run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def make_tcp_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    listener.listen(8)
    listener.setblocking(False)
    return listener, listener.getsockname()[1]


def control_pair():
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)


async def echo_handler(ws, path):
    message = await ws.recv()
    await ws.send(f'echo:{message}')
