# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio


def is_pending(task: asyncio.Task | None) -> bool:
    return task is not None and not task.done()
