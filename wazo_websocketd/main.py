# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from wazo_websocketd.config import load_config
from wazo_websocketd.controller import Controller
from wazo_websocketd.helpers.process import bootstrap, drop_privileges


def main():
    config = load_config()

    bootstrap(config, 'websocketd')
    drop_privileges(config)

    Controller(config).run()
