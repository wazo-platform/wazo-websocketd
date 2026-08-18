# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from signal import SIGKILL, SIGTERM

from setproctitle import setproctitle
from xivo.config_helper import set_xivo_uuid
from xivo.user_rights import change_user

from wazo_websocketd.helpers.logs import setup_process_logging

logger = logging.getLogger(__name__)


def is_root() -> bool:
    return os.getuid() == 0


def drop_privileges(config: dict) -> None:
    if config['user'] and is_root():
        change_user(config['user'])


def bootstrap(config: dict, name: str) -> None:
    """Prepare a process to run as `name`, still holding its privileges."""
    setup_process_logging(config, name)
    set_xivo_uuid(config, logger)
    setproctitle(f'wazo-websocketd: {name}')


def child_command(config: dict, executable: str, name: str) -> list[str]:
    command = [executable, '--supervised-as', name]
    if config_file := config.get('config_file'):
        command += ['-c', config_file]
    if config.get('debug'):
        command.append('-d')
    return command


async def spawn(command: list[str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(*command)


def child_identity(config: dict, default_name: str) -> tuple[str, bool]:
    supervised_as = config.get('supervised_as')
    return supervised_as or default_name, bool(supervised_as)


async def stop_pids(
    pids: list[int], *, grace: float, interval: float, label: str
) -> None:
    deadline = time.monotonic() + grace
    while pids and time.monotonic() < deadline:
        pids = [pid for pid in pids if is_pid_running(pid)]
        if not pids:
            return
        await asyncio.sleep(interval)

    for pid in pids:
        logger.warning('adopted %s %s did not stop gracefully, killing it', label, pid)
        signal_pid(pid, SIGKILL)


async def stop_children(
    children: list[asyncio.subprocess.Process], *, grace: float, label: str
) -> None:
    if not (waits := [asyncio.ensure_future(child.wait()) for child in children]):
        return

    _, pending = await asyncio.wait(waits, timeout=grace)
    if pending:
        logger.warning(
            '%d %s did not stop gracefully, killing them', len(pending), label
        )
        for child in children:
            signal_process(child, SIGKILL)
        await asyncio.gather(*waits, return_exceptions=True)


def is_pid_running(pid: int) -> bool:
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    return not reaped


def signal_pid(pid: int, signal: int = SIGTERM) -> None:
    with contextlib.suppress(OSError):
        os.kill(pid, signal)


def signal_process(process: asyncio.subprocess.Process, signal: int = SIGTERM) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.send_signal(signal)
