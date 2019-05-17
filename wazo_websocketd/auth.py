# Copyright 2016-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import datetime
import logging

import requests
import wazo_auth_client

from .exception import (
    AuthenticationError,
    AuthenticationExpiredError,
)

logger = logging.getLogger(__name__)


def new_authenticator(config, loop):
    auth_client = wazo_auth_client.Client(**config['auth'])
    websocketd_auth_client = _WebSocketdAuthClient(loop, auth_client)
    strategy_name = config['auth_check_strategy']
    if strategy_name == 'static':
        interval = config['auth_check_static_interval']
        auth_check = _StaticIntervalAuthCheck(loop, websocketd_auth_client, interval)
    elif strategy_name == 'dynamic':
        auth_check = _DynamicIntervalAuthCheck(loop, websocketd_auth_client)
    else:
        raise Exception('unknown auth_check_strategy {}'.format(strategy_name))
    return _Authenticator(websocketd_auth_client, auth_check)


class _WebSocketdAuthClient(object):

    _ACL = 'websocketd'

    def __init__(self, loop, auth_client):
        self._loop = loop
        self._auth_client = auth_client

    @asyncio.coroutine
    def get_token(self, token_id):
        logger.debug('getting token from wazo-auth')
        try:
            return (yield from self._loop.run_in_executor(None, self._auth_client.token.get, token_id, self._ACL))
        except requests.RequestException as e:
            # there's currently no clean way with wazo_auth_client to know if the
            # error was caused because the token is unauthorized, or unknown
            # or something else
            raise AuthenticationError(e)

    @asyncio.coroutine
    def is_valid_token(self, token_id, acl=_ACL):
        logger.debug('checking token validity from wazo-auth')
        return (yield from self._loop.run_in_executor(None, self._auth_client.token.is_valid, token_id, acl))


class _Authenticator(object):

    def __init__(self, websocketd_auth_client, auth_check):
        self._websocketd_auth_client = websocketd_auth_client
        self._auth_check = auth_check

    def get_token(self, token_id):
        # This function returns a coroutine.
        return self._websocketd_auth_client.get_token(token_id)

    def is_valid_token(self, token_id, acl=None):
        # This function returns a coroutine.
        return self._websocketd_auth_client.is_valid_token(token_id, acl)

    def run_check(self, token):
        # This function returns a coroutine that raise an AuthenticationExpiredError exception
        # when the token expires.
        return self._auth_check.run(token)


class _StaticIntervalAuthCheck(object):

    def __init__(self, loop, websocketd_auth_client, interval):
        self._loop = loop
        self._websocketd_auth_client = websocketd_auth_client
        self._interval = interval

    @asyncio.coroutine
    def run(self, token):
        token_id = token['token']
        while True:
            yield from asyncio.sleep(self._interval, loop=self._loop)
            logger.debug('static auth check: testing token validity')
            is_valid = yield from self._websocketd_auth_client.is_valid_token(token_id)
            if not is_valid:
                raise AuthenticationExpiredError()


class _DynamicIntervalAuthCheck(object):

    _ISO_DATETIME = '%Y-%m-%dT%H:%M:%S.%f'

    def __init__(self, loop, websocketd_auth_client):
        self._loop = loop
        self._websocketd_auth_client = websocketd_auth_client

    @asyncio.coroutine
    def run(self, token):
        token_id = token['token']
        while True:
            # FIXME if wazo-websocketd and wazo-auth are not in the same
            #       timezone, this doesn't work -- but this needs to be fixed
            #       in wazo-auth, which should returns data in UTC instead of
            #       in local time
            now = datetime.datetime.now()
            expires_at = datetime.datetime.strptime(token['expires_at'], self._ISO_DATETIME)
            next_check = self._calculate_next_check(now, expires_at)
            yield from asyncio.sleep(next_check, loop=self._loop)
            logger.debug('dynamic auth check: testing token validity')
            try:
                token = yield from self._websocketd_auth_client.get_token(token_id)
            except AuthenticationError:
                raise AuthenticationExpiredError()

    def _calculate_next_check(self, now, expires_at):
        delta = expires_at - now
        delta_seconds = delta.total_seconds()
        if delta_seconds < 0:
            return 15
        elif delta_seconds <= 80:
            return 60
        elif delta_seconds <= 57600:
            return int(0.75 * delta_seconds)
        return 43200
