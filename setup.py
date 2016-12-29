#!/usr/bin/env python3
# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

from setuptools import setup
from setuptools import find_packages


setup(
    name='xivo-websocketd',
    version='1.0',
    author='Wazo Authors',
    author_email='dev.wazo@gmail.com',
    url='http://wazo.community',
    packages=find_packages(),
    scripts=['bin/xivo-websocketd'],
)
