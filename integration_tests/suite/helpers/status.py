# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json

READ_STATUSES = '''
import glob
import json
import socket

for path in sorted(glob.glob('/run/wazo-websocketd/*.sock')):
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(2)
    try:
        client.connect(path)
        client.sendall(
            b'GET /status HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n'
        )
        response = b''
        while chunk := client.recv(4096):
            response += chunk
    except OSError:
        continue
    finally:
        client.close()

    try:
        status = json.loads(response.split(b'\\r\\n\\r\\n', 1)[-1])
    except ValueError:
        continue
    print(json.dumps(status))
'''


def parse_statuses(output: bytes) -> dict[str, dict]:
    statuses = {}
    for line in output.decode().splitlines():
        if not line.startswith('{'):
            continue
        status = json.loads(line)
        statuses[status['name']] = status
    return statuses
