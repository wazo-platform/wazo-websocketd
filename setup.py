#!/usr/bin/env python3
# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from setuptools import setup
from setuptools import find_packages


NAME = 'wazo-websocketd'
setup(
    name=NAME,
    version='1.0',
    author='Wazo Authors',
    author_email='dev@wazo.community',
    url='http://wazo.community',
    packages=find_packages(),
    entry_points={'console_scripts': [f'{NAME}=wazo_websocketd.main:main']},
)
