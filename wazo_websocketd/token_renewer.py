# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from wazo_auth_client.types import TokenDict

from .auth import AsyncAuthClient
from .helpers.tasks import is_pending
from .helpers.timing import capped_backoff

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
        self._task: asyncio.Task[None] = None  # type: ignore[assignment]

    async def start(self) -> ServiceTokenRenewer:
        if is_pending(self._task):
            raise RuntimeError('service token renewer is already running')

        logger.info('service token renewer started')
        self._task = asyncio.create_task(self._run())
        return self

    async def stop(self, *_args: Any) -> None:
        if is_pending(self._task):
            self._task.cancel()
            logger.info('service token renewer stopped')

        # shielded: stopping from a subscriber cancels the task running this
        await asyncio.shield(self._client.close())

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
        delays = capped_backoff(1, 5, 32)
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
        self._callbacks = [callback for callback in callbacks if not callback.oneshot]
        for callback in callbacks:
            payload = token if callback.details else token['token']
            if asyncio.iscoroutinefunction(callback.method):
                await callback.method(payload)
            else:
                asyncio.get_running_loop().call_soon(callback.method, payload)
