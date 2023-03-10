# Copyright 2016-2023 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
import datetime
import logging
import requests

from collections import namedtuple
from functools import partial
from itertools import chain, repeat
from multiprocessing import Array, Event
from typing import Callable, Dict, List
from wazo_auth_client import Client as AuthClient

from .exception import AuthenticationError, AuthenticationExpiredError

logger = logging.getLogger(__name__)
_master_tenant_initialized = Event()
_master_tenant_uuid = Array('c', 36, lock=True)


def set_master_tenant(token: Dict) -> None:
    try:
        tenant_uuid = token['metadata']['tenant_uuid']
    except KeyError:
        logger.error('invalid token, contains no tenant_uuid')
    else:
        logger.info('setting master_tenant_uuid to \'%s\'', tenant_uuid)
        with _master_tenant_uuid:
            _master_tenant_initialized.set()
            _master_tenant_uuid.value = str.encode(tenant_uuid)


def get_master_tenant() -> str:
    if not _master_tenant_initialized.is_set():
        return None
    with _master_tenant_uuid:
        return str(_master_tenant_uuid.value, encoding='utf-8')


def has_master_tenant() -> bool:
    return get_master_tenant() is not None


class AsyncAuthClient:
    _ACL = 'websocketd'

    def __init__(self, config):
        self._auth_client = AuthClient(**config['auth'])

    async def get_token(self, token_id):
        logger.debug('getting token from wazo-auth')
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                None, self._auth_client.token.get, token_id, self._ACL
            )
        except requests.RequestException as e:
            # there's currently no clean way with wazo_auth_client to know if the
            # error was caused because the token is unauthorized, or unknown
            # or something else
            raise AuthenticationError(e)

    async def is_valid_token(self, token_id, acl=_ACL):
        logger.debug('checking token validity from wazo-auth')
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._auth_client.token.is_valid, token_id, acl
        )


class _StaticIntervalAuthCheck:
    def __init__(self, async_auth_client, config):
        self._async_auth_client = async_auth_client
        self._interval = config['auth_check_static_interval']

    async def run(self, token_getter):
        while True:
            await asyncio.sleep(self._interval)
            logger.debug('static auth check: testing token validity')
            token_id = token_getter()['token']
            is_valid = await self._async_auth_client.is_valid_token(token_id)
            if not is_valid:
                raise AuthenticationExpiredError()


class _DynamicIntervalAuthCheck:
    _ISO_DATETIME = '%Y-%m-%dT%H:%M:%S.%f'

    def __init__(self, async_auth_client, config):
        self._async_auth_client = async_auth_client

    async def run(self, token_getter):
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
            await asyncio.sleep(next_check)
            logger.debug('dynamic auth check: testing token validity')
            try:
                await self._async_auth_client.get_token(token_id)
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


class Authenticator:
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


class ServiceTokenRenewer:
    DEFAULT_EXPIRATION = 21600  # 6h
    DEFAULT_LEEWAY_FACTOR = 0.85

    Callback = namedtuple('Callback', ['method', 'details', 'oneshot'])

    def __init__(self, config: Dict, *, loop: asyncio.AbstractEventLoop = None):
        self._callbacks: List[ServiceTokenRenewer.Callback] = []
        self._client = AuthClient(**config['auth'])
        self._expiration: int = self.DEFAULT_EXPIRATION
        self._lock = asyncio.Lock()
        self._loop = loop or asyncio.get_event_loop()
        self._task: asyncio.Task = None

    async def __aenter__(self):
        logger.info('service token renewer started')
        self._task = self._loop.create_task(self._run())
        return self

    async def __aexit__(self, *args):
        if not self._task.cancelled():
            self._task.cancel()
        logger.info('service token renewer stopped')

    def subscribe(
        self,
        callback: Callable[[str], None],
        *,
        details: bool = False,
        oneshot: bool = False
    ) -> None:
        callback_ = self.Callback(callback, details, oneshot)
        self._callbacks.append(callback_)

    def unsubscribe(
        self,
        callback: Callable[[str], None],
        *,
        details: bool = False,
        oneshot: bool = False
    ) -> None:
        callback_ = self.Callback(callback, details, oneshot)
        try:
            self._callbacks.remove(callback_)
        except ValueError:
            pass

    async def _run(self):
        while True:
            token = await self._fetch_token()
            await self._notify(token)
            await asyncio.sleep(self._expiration * self.DEFAULT_LEEWAY_FACTOR)

    async def _fetch_token(self) -> Dict:
        timeouts = chain((1, 2, 4, 8, 16), repeat(32))
        fn = partial(self._client.token.new, expiration=self._expiration)
        while True:
            try:
                return await self._loop.run_in_executor(None, fn)
            except Exception as exc:
                interval = next(timeouts)
                await self.on_error(exc, interval)
            await asyncio.sleep(interval)

    async def _notify(self, token: Dict):
        callbacks = self._callbacks.copy()
        for callback in callbacks:
            if callback.oneshot:
                async with self._lock:
                    self._callbacks.remove(callback)
            payload = token if callback.details else token['token']
            self._loop.call_soon(callback.method, payload)

    async def on_error(self, exc: Exception, interval: int):
        logger.error(
            'Failed to create an access token, retrying in %d seconds',
            interval,
        )
