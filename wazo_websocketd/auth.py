# Copyright 2016-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import datetime
import logging

import requests
import wazo_auth_client

from .exception import AuthenticationError, AuthenticationExpiredError

logger = logging.getLogger(__name__)


class AsyncAuthClient(object):

    _ACL = 'websocketd'

    def __init__(self, config):
        self._auth_client = wazo_auth_client.Client(**config['auth'])

    @asyncio.coroutine
    def get_token(self, token_id):
        logger.debug('getting token from wazo-auth')
        loop = asyncio.get_event_loop()
        try:
            return (
                yield from loop.run_in_executor(
                    None, self._auth_client.token.get, token_id, self._ACL
                )
            )
        except requests.RequestException as e:
            # there's currently no clean way with wazo_auth_client to know if the
            # error was caused because the token is unauthorized, or unknown
            # or something else
            raise AuthenticationError(e)

    @asyncio.coroutine
    def is_valid_token(self, token_id, acl=_ACL):
        logger.debug('checking token validity from wazo-auth')
        loop = asyncio.get_event_loop()
        return (
            yield from loop.run_in_executor(
                None, self._auth_client.token.is_valid, token_id, acl
            )
        )


class _StaticIntervalAuthCheck(object):
    def __init__(self, async_auth_client, config):
        self._async_auth_client = async_auth_client
        self._interval = config['auth_check_static_interval']

    @asyncio.coroutine
    def run(self, token_getter):
        while True:
            token_id = token_getter()['token']
            yield from asyncio.sleep(self._interval)
            logger.debug('static auth check: testing token validity')
            is_valid = yield from self._async_auth_client.is_valid_token(token_id)
            if not is_valid:
                raise AuthenticationExpiredError()


class _DynamicIntervalAuthCheck(object):

    _ISO_DATETIME = '%Y-%m-%dT%H:%M:%S.%f'

    def __init__(self, async_auth_client, config):
        self._async_auth_client = async_auth_client

    @asyncio.coroutine
    def run(self, token_getter):
        while True:
            token = token_getter()
            token_id = token['token']
            # FIXME if wazo-websocketd and wazo-auth are not in the same
            #       timezone, this doesn't work -- but this needs to be fixed
            #       in wazo-auth, which should returns data in UTC instead of
            #       in local time
            now = datetime.datetime.now()
            expires_at = datetime.datetime.strptime(
                token['expires_at'], self._ISO_DATETIME
            )
            next_check = self._calculate_next_check(now, expires_at)
            yield from asyncio.sleep(next_check)
            logger.debug('dynamic auth check: testing token validity')
            try:
                yield from self._async_auth_client.get_token(token_id)
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


STRATEGIES = {'static': _StaticIntervalAuthCheck, 'dynamic': _DynamicIntervalAuthCheck}


class Authenticator(object):
    def __init__(self, config):
        self._async_auth_client = AsyncAuthClient(config)
        auth_check_class = STRATEGIES.get(config['auth_check_strategy'])
        if not auth_check_class:
            raise Exception(
                'unknown auth_check_strategy {}'.format(config['auth_check_strategy'])
            )
        self._auth_check = auth_check_class(self._async_auth_client, config)

    def get_token(self, token_id):
        # This function returns a coroutine.
        return self._async_auth_client.get_token(token_id)

    def is_valid_token(self, token_id, acl=None):
        # This function returns a coroutine.
        return self._async_auth_client.is_valid_token(token_id, acl)

    def run_check(self, token_getter):
        # This function returns a coroutine that raise an AuthenticationExpiredError exception
        # when the token expires.
        return self._auth_check.run(token_getter)
