# Copyright 2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from wazo_test_helpers.pytest_asset import (
    asset_fixture,
    enable_mark_logs_fixture,
    register,
)

from .helpers import base as asset


def pytest_configure(config):
    register(config)


base = asset_fixture(asset.BaseAssetLaunchingTestCase)
static_auth_check = asset_fixture(asset.StaticAuthCheckAssetLaunchingTestCase)
standalone = asset_fixture(asset.StandaloneAssetLaunchingTestCase)

mark_logs = enable_mark_logs_fixture()
