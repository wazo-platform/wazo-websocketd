# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import logging

from xivo import xivo_logging

_NOISY_LOGGERS = ['urllib3', 'aioamqp', 'aiohttp', 'stevedore.extension']


class _ProcessLabelFilter(logging.Filter):
    def __init__(self, name: str) -> None:
        super().__init__()
        self._label = f'[{name}]'

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, 'labelled', False):
            record.msg = f'{self._label} {record.msg}'
            record.labelled = True  # type: ignore[attr-defined]
        return True


def _stamp_process_name(name: str) -> None:
    label = _ProcessLabelFilter(name)
    for handler in logging.getLogger().handlers:
        handler.addFilter(label)


def setup_process_logging(config: dict, name: str) -> None:
    """Configure logging for a process and prefix its lines with `name`."""
    xivo_logging.setup_logging(
        config['log_file'], debug=config['debug'], log_level=config['log_level']
    )
    xivo_logging.silence_loggers(_NOISY_LOGGERS, logging.WARNING)
    _stamp_process_name(name)
