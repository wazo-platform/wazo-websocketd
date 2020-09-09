# Copyright 2016-2020 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import re

# TODO
# - move into it's own module that is used by both websocketd
#   and wazo-auth
# - also move the test module
# - use it in wazo-auth


class ACLCheck(object):
    def __init__(self, user_uuid, acls):
        self._positive_acl_regexes = [
            self._transform_acl_to_regex(user_uuid, acl)
            for acl in acls
            if not acl.startswith('!')
        ]
        self._negative_acl_regexes = [
            self._transform_acl_to_regex(user_uuid, acl[1:])
            for acl in acls
            if acl.startswith('!')
        ]

    def matches_required_acl(self, required_acl):
        if required_acl is None:
            return True

        for acl_regex in self._negative_acl_regexes:
            if acl_regex.match(required_acl):
                return False

        for acl_regex in self._positive_acl_regexes:
            if acl_regex.match(required_acl):
                return True
        return False

    @staticmethod
    def _transform_acl_to_regex(user_uuid, acl):
        if acl.endswith('.me'):
            acl = '{}.{}'.format(acl[:-3], user_uuid)
        else:
            acl = acl.replace('.me.', '.{}.'.format(user_uuid))

        acl_regex = re.escape(acl).replace('\\*', '[^.]*?').replace('\\#', '.*?')
        return re.compile('^{}$'.format(acl_regex))
