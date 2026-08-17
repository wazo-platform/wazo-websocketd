# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging
import os
import pwd

from setproctitle import setproctitle
from xivo.config_helper import set_xivo_uuid
from xivo.user_rights import change_user

from wazo_websocketd.helpers.logs import setup_process_logging

logger = logging.getLogger(__name__)

RUN_DIR = '/run/wazo-websocketd'


def is_root() -> bool:
    return os.getuid() == 0


def ensure_runtime_dir(config: dict) -> None:
    os.makedirs(RUN_DIR, mode=0o750, exist_ok=True)
    if config['user'] and is_root():
        entry = pwd.getpwnam(config['user'])
        os.chown(RUN_DIR, entry.pw_uid, entry.pw_gid)


def drop_privileges(config: dict) -> None:
    if config['user'] and is_root():
        change_user(config['user'])


def bootstrap(config: dict, name: str) -> None:
    """Prepare a process to run as `name`, still holding its privileges."""
    setup_process_logging(config, name)
    ensure_runtime_dir(config)
    set_xivo_uuid(config, logger)
    setproctitle(f'wazo-websocketd: {name}')


def child_identity(config: dict, default_name: str) -> tuple[str, bool]:
    supervised_as = config.get('supervised_as')
    return supervised_as or default_name, bool(supervised_as)
