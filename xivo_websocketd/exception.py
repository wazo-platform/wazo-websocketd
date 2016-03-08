# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+


class NoTokenError(Exception):
    pass


class AuthenticationError(Exception):
    pass


class AuthenticationExpiredError(AuthenticationError):
    pass


class SessionProtocolError(Exception):
    pass


class BusConnectionError(Exception):
    pass


class BusConnectionLostError(BusConnectionError):
    pass
