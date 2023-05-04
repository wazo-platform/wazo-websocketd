# Copyright 2023-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import asyncio
import sys

from asyncio import TimeoutError
from wazo_websocketd.config import _DEFAULT_CONFIG
from websockets.client import connect
from xivo.chain_map import ChainMap
from xivo.config_helper import read_config_file_hierarchy


HOST = 'localhost'
RETRY_INTERVAL = 0.5
TIMEOUT = 60


async def wait_opened(host: str, port: int, timeout: float, interval: float):
    async def retry_connection(host: str, port: int, interval: float) -> bool:
        while True:
            try:
                connection = await connect(f'ws://{host}:{port}')
            except ConnectionRefusedError:
                await asyncio.sleep(interval)
                continue
            else:
                await connection.close(code=1000, reason='websocketd is up')
                return

    await asyncio.wait_for(retry_connection(host, port, interval), timeout)


def get_websocketd_port() -> int:
    file_config = read_config_file_hierarchy(_DEFAULT_CONFIG)
    config = ChainMap(file_config, _DEFAULT_CONFIG)
    return config['websocket']['port']


def main():
    port = get_websocketd_port()

    try:
        asyncio.run(wait_opened(HOST, port, TIMEOUT, RETRY_INTERVAL))
    except (KeyboardInterrupt, TimeoutError):
        print(f'could not connect to wazo-websocketd on port {port}', file=sys.stderr)
        exit(1)


if __name__ == '__main__':
    main()
