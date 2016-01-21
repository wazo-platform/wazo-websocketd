#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2016 by Avencall
# SPDX-License-Identifier: GPL-3.0+

from setuptools import setup
from setuptools import find_packages


setup(
    name='xivo-websocketd',
    version='1.0',
    author='Avencall',
    author_email='xivo-dev@lists.proformatique.com',
    url='http://www.xivo.io/',
    packages=find_packages(),
    scripts=['bin/xivo-websocketd'],
)
