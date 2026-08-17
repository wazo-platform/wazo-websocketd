# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import asyncio
import json
import logging
from secrets import token_hex
from typing import TYPE_CHECKING, NamedTuple

from aioamqp.channel import Channel
from aioamqp.exceptions import AioamqpException
from aioamqp.properties import Properties
from wazo_auth_client.types import TokenDict
from xivo.auth_verifier import AccessCheck

from .auth import MasterTenant
from .bus import BusConnection, _BusConsumer, generate_name
from .exception import (
    AuthenticationError,
    BusConnectionError,
    BusConnectionLostError,
    EventPermissionError,
    InvalidEvent,
    InvalidTokenError,
    SessionRevokedError,
)
from .helpers.timing import capped_backoff
from .helpers.token import TokenUser

if TYPE_CHECKING:
    from .token_cache import CachingAuthenticator

logger = logging.getLogger(__name__)

SESSION_DELETED_EVENT = 'auth_session_deleted'


def parse_event_session_uuid(body: bytes | str) -> str | None:
    try:
        message = json.loads(body)
        data = message.get('data') or message
        return data['uuid']
    except (ValueError, KeyError, TypeError, AttributeError):
        logger.warning('discarding malformed `%s` event', SESSION_DELETED_EVENT)
        return None


class BusEvent(NamedTuple):
    name: str
    headers: dict
    acl: str | None
    content: dict
    raw: str


class UserBusSubscriber:
    """Serves one websocket session the events its user is allowed to see."""

    def __init__(self, connection: BusConnection, config: dict, token: TokenDict):
        user = self._read_token(token)
        self._set_user(user)
        self._origin_uuid: str = config['uuid']
        self._exchange_name: str = config['bus']['exchange_name']
        self._tenant_exchange = self._own_tenant_exchange()
        self._subscriptions: set[str] = set()
        self._consumer = _BusConsumer(
            connection,
            queue_name=generate_name(f'user-{self._user.uuid}', token_hex(3)),
            exchange=self._tenant_exchange or self._exchange_name,
            handler=self._decode,
            wait=False,  # a session must not wait on a bus that is down
            bindings=[
                {
                    'name': SESSION_DELETED_EVENT,
                    f'user_uuid:{self._user.uuid}': True,
                }
            ],
            prefetch=config['bus']['consumer_prefetch'],
            on_setup=self._declare_tenant_exchange if self._tenant_exchange else None,
        )

    async def __aenter__(self) -> UserBusSubscriber:
        await self._consumer.__aenter__()
        return self

    async def __aexit__(self, *args) -> None:
        await self._consumer.__aexit__(*args)

    def __aiter__(self) -> UserBusSubscriber:
        return self

    async def __anext__(self) -> BusEvent:
        return await self._consumer.__anext__()

    async def subscribe(self, event_name: str) -> None:
        self._subscriptions.add(event_name)
        for binding in self._generate_bindings(event_name):
            await self._consumer.bind(binding)

    async def unsubscribe(self, event_name: str) -> None:
        self._subscriptions.discard(event_name)
        for binding in self._generate_bindings(event_name):
            await self._consumer.unbind(binding)

    def get_token(self) -> dict[str, str]:
        return {
            'token': self._user.token_id,
            'utc_expires_at': self._user.token_utc_expires_at,
        }

    def set_token(self, token: TokenDict) -> None:
        user = self._read_token(token)
        if user.uuid != self._user.uuid:
            raise AuthenticationError('token belongs to another user')

        self._set_user(user)

    @staticmethod
    def _read_token(token: TokenDict) -> TokenUser:
        if 'metadata' not in token:
            raise InvalidTokenError('Malformed token received, missing token details')
        return TokenUser(token, MasterTenant.matches)

    def _set_user(self, user: TokenUser) -> None:
        self._user = user
        self._access = AccessCheck(user.uuid, user.session_uuid, user.acl)

    def _own_tenant_exchange(self) -> str | None:
        """The exchange this user's events reach it through, or None for the main one."""
        if self._user.is_master_tenant():
            logger.debug('user `%s` connected as global admin', self._user.uuid)
            return None

        if self._user.is_admin():
            logger.debug('user `%s` connected as tenant\'s admin', self._user.uuid)
        else:
            logger.debug('user `%s` connected as user', self._user.uuid)

        return generate_name(f'tenant-{self._user.tenant_uuid}')

    async def _declare_tenant_exchange(self, channel: Channel) -> None:
        await channel.exchange(
            self._tenant_exchange, 'headers', durable=False, auto_delete=True
        )

        await channel.exchange_bind(
            self._tenant_exchange,
            self._exchange_name,
            '',
            arguments={
                'origin_uuid': self._origin_uuid,
                'tenant_uuid': self._user.tenant_uuid,
            },
        )

    def _decode(
        self, content: bytes, properties: Properties
    ) -> BusEvent | Exception | None:
        if self._is_revocation(content, properties):
            logger.info(
                'session was deleted, closing the connection (user=%s tenant=%s)',
                self._user.uuid,
                self._user.tenant_uuid,
            )
            return SessionRevokedError()

        try:
            event = self._decode_content(content, properties)
        except InvalidEvent as exc:
            logger.error('error during message decoding (reason: %s)', exc)
        except EventPermissionError as exc:
            logger.debug('discarding event (reason: %s)', exc)
        else:
            if self._is_subscribed(event.name):
                return event
        return None

    def _is_revocation(self, content: bytes, properties: Properties) -> bool:
        if (properties.headers or {}).get('name') != SESSION_DELETED_EVENT:
            return False
        return parse_event_session_uuid(content) == self._user.session_uuid

    def _is_subscribed(self, event_name: str) -> bool:
        return '*' in self._subscriptions or event_name in self._subscriptions

    def _decode_content(self, content: bytes, properties: Properties) -> BusEvent:
        headers = properties.headers
        try:
            decoded = content.decode('utf-8')
            message = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InvalidEvent('unable to decode message')

        if not isinstance(message, dict):
            raise InvalidEvent('invalid message format (not a dict)')

        event_name = headers.get('name') or message.get('name')
        if not event_name:
            raise InvalidEvent('event is missing `name` field')

        if 'required_acl' not in headers:
            raise EventPermissionError(f'event `{event_name}` doesn\'t contain ACLs`')
        acl = headers.get('required_acl')

        if isinstance(acl, bytes):
            acl = acl.decode('utf-8')
        if acl and not isinstance(acl, str):
            raise InvalidEvent(
                'event ACL is not a string (type: %s)', type(acl).__name__
            )

        if not self._has_access(acl):
            raise EventPermissionError(
                f'user `{self._user.uuid}` doesn\'t have '
                f'the required ACL for event `{event_name}` (missing: {acl})'
            )

        return BusEvent(event_name, headers, acl, message, decoded)

    def _generate_bindings(self, event_name: str) -> list[dict]:
        binding = {}
        if event_name != '*':
            binding['name'] = event_name

        if self._user.is_admin():
            binding['origin_uuid'] = self._origin_uuid
            return [binding]

        # note: users don't need origin_uuid because the tenant exchange takes care of it
        return [
            binding | {f'user_uuid:{self._user.uuid}': True},
            binding | {'user_uuid:*': True},
        ]

    def _has_access(self, acl: str) -> bool:
        return self._access.matches_required_access(acl)


class SessionInvalidator:
    """Evicts cached tokens whose session wazo-auth has deleted."""

    _RETRY_DELAY = 1.0
    _RETRY_CEILING = 32.0
    _RETRIES = 5

    def __init__(
        self,
        connection: BusConnection,
        config: dict,
        cache: CachingAuthenticator,
    ) -> None:
        self._connection = connection
        self._exchange_name: str = config['bus']['exchange_name']
        self._exchange_type: str = config['bus']['exchange_type']
        self._cache = cache
        self._consumer = _BusConsumer(
            connection,
            queue_name=generate_name(f'broker-{token_hex(3)}'),
            exchange=self._exchange_name,
            handler=self._on_deleted_session,
            wait=True,  # started with the bus, so let the connection come up
            prefetch=config['bus']['consumer_prefetch'],
            bindings=[{'name': SESSION_DELETED_EVENT}],
            on_setup=self._declare_exchange,
        )

    async def run(self) -> None:
        try:
            await self._run()
        except Exception:
            logger.exception('bus session eviction stopped')
            raise
        finally:
            self._cache.eviction_lost()

    async def _run(self) -> None:
        delays = self._delays()
        while True:
            try:
                async with self._consumer:
                    delays = self._delays()
                    self._cache.eviction_active()
                    logger.info('bus session eviction active')
                    try:
                        async for session_uuid in self._consumer:
                            self._cache.evict_session(session_uuid)
                    finally:
                        self._cache.eviction_lost()
            except (
                BusConnectionError,
                BusConnectionLostError,
                AioamqpException,
                OSError,
            ) as e:
                if self._connection.is_closing:
                    return
                delay = next(delays)
                logger.warning(
                    'could not subscribe to bus events, retrying in %ds (reason: %s)',
                    delay,
                    e,
                )
                await asyncio.sleep(delay)

    def _delays(self):
        return capped_backoff(self._RETRY_DELAY, self._RETRIES, self._RETRY_CEILING)

    async def _declare_exchange(self, channel: Channel) -> None:
        await channel.exchange(self._exchange_name, self._exchange_type, durable=True)

    def _on_deleted_session(
        self, content: bytes, _properties: Properties
    ) -> str | None:
        return parse_event_session_uuid(content)
