# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from itertools import chain, repeat
from typing import Any, NamedTuple

from wazo_auth_client.types import TokenDict

from .auth import AsyncAuthClient
from .helpers.tasks import is_pending
from .helpers.timing import exponential_backoff

logger = logging.getLogger(__name__)


class ServiceTokenRenewer:
    DEFAULT_EXPIRATION = 21600  # 6h
    DEFAULT_LEEWAY_FACTOR = 0.85

    class Callback(NamedTuple):
        method: Callable[..., None | Awaitable[None]]
        details: bool
        oneshot: bool

    def __init__(self, config: dict[str, Any]) -> None:
        self._callbacks: list[ServiceTokenRenewer.Callback] = []
        self._client = AsyncAuthClient(config['auth'])
        self._expiration: int = self.DEFAULT_EXPIRATION
        self._lock = asyncio.Lock()
        self._loop = asyncio.get_event_loop()
        self._task: asyncio.Task[None] = None  # type: ignore[assignment]

    async def start(self) -> ServiceTokenRenewer:
        if is_pending(self._task):
            raise RuntimeError('service token renewer is already running')

        logger.info('service token renewer started')
        self._task = self._loop.create_task(self._run())
        return self

    async def stop(self, *_args: Any) -> None:
        if not is_pending(self._task):
            return

        self._task.cancel()
        await self._client.close()
        logger.info('service token renewer stopped')

    async def __aenter__(self) -> ServiceTokenRenewer:
        return await self.start()

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()

    def subscribe(
        self,
        callback: Callable[..., None | Awaitable[None]],
        *,
        details: bool = False,
        oneshot: bool = False,
    ) -> None:
        callback_ = self.Callback(callback, details, oneshot)
        self._callbacks.append(callback_)

    def unsubscribe(
        self,
        callback: Callable[..., None | Awaitable[None]],
        *,
        details: bool = False,
        oneshot: bool = False,
    ) -> None:
        callback_ = self.Callback(callback, details, oneshot)
        try:
            self._callbacks.remove(callback_)
        except ValueError:
            pass

    async def _run(self) -> None:
        while True:
            token = await self._fetch_token()
            await self._notify(token)
            await asyncio.sleep(self._expiration * self.DEFAULT_LEEWAY_FACTOR)

    async def _fetch_token(self) -> TokenDict:
        delays = chain(exponential_backoff(1, 5), repeat(32))
        while True:
            try:
                return await self._client.create_token(self._expiration)
            except Exception as error:
                delay = next(delays)
                logger.error(
                    'could not create a service token, retrying in %ds (reason: %s)',
                    delay,
                    error,
                )
            await asyncio.sleep(delay)

    async def _notify(self, token: TokenDict) -> None:
        callbacks = self._callbacks.copy()
        for callback in callbacks:
            if callback.oneshot:
                async with self._lock:
                    self._callbacks.remove(callback)
            payload = token if callback.details else token['token']
            if asyncio.iscoroutinefunction(callback.method):
                await callback.method(payload)
            else:
                self._loop.call_soon(callback.method, payload)
