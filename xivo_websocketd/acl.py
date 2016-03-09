# Copyright 2016 Avencall
# SPDX-License-Identifier: GPL-3.0+

import re

# TODO
# - move into it's own module that is used by both websocketd
#   and xivo-auth
# - also move the test module
# - make sure it's python 2 AND python3 compatible
# - modify it so it "user_acl_regex.match" instead of "re.match"
#   and transform the ACL to regex only once (lazily so that we
#   have a similar behaviour for xivo-auth?)
# - use it in xivo-auth


class ACLCheck(object):

    def __init__(self, auth_id, acls):
        self._auth_id = auth_id
        self._acls = acls

    def matches_required_acl(self, required_acl):
        if required_acl is None:
            return True

        for user_acl in self._acls:
            if user_acl.endswith('.me'):
                user_acl = '{}.{}'.format(user_acl[:-3], self._auth_id)
            else:
                user_acl = user_acl.replace('.me.', '.{}.'.format(self._auth_id))

            user_acl_regex = self._transform_acl_to_regex(user_acl)
            if re.match(user_acl_regex, required_acl):
                return True
        return False

    def _transform_acl_to_regex(self, acl):
        acl_regex = re.escape(acl).replace('\*', '[^.]*?').replace('\#', '.*?')
        return re.compile('^{}$'.format(acl_regex))
