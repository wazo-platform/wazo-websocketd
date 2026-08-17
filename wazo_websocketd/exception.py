# Copyright 2016-2026 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

from aiohttp import ClientResponseError


class _AuthError(Exception):
    def __init__(self, *args):
        super().__init__(
            *(
                self._format_exception(arg) if isinstance(arg, BaseException) else arg
                for arg in args
            )
        )

    @staticmethod
    def _format_exception(error: BaseException) -> str:
        """Describe a client error without its URL, which carries the token id."""
        if isinstance(error, ClientResponseError):
            return f'{error.status} {error.message}'
        return f'{type(error).__name__}: {error}'


class NoTokenError(Exception):
    pass


class InvalidTokenError(Exception):
    pass


class AuthenticationError(_AuthError):
    def __init__(self, *args, status_code: int = 401):
        super().__init__(*args)
        self.status_code = status_code


class AuthenticationExpiredError(AuthenticationError):
    pass


class AuthServerUnavailableError(_AuthError):
    pass


class AuthServerUnreachableError(AuthServerUnavailableError):
    pass


class SessionProtocolError(Exception):
    pass


class BusConnectionError(Exception):
    pass


class BusConnectionLostError(BusConnectionError):
    pass


class UnsupportedVersionError(Exception):
    pass


class InvalidEvent(Exception):
    pass


class EventPermissionError(Exception):
    pass
