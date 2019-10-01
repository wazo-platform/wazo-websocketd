# Copyright 2016-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest

from hamcrest import assert_that, equal_to
from ..acl import ACLCheck


class TestACLCheck(unittest.TestCase):
    def setUp(self):
        self.user_uuid = '123'

    def test_matches_required_acls_when_user_acl_ends_with_hashtag(self):
        acls = ['foo.bar.#']
        acl_check = ACLCheck(self.user_uuid, acls)

        assert_that(acl_check.matches_required_acl('foo.bar'), equal_to(False))
        assert_that(acl_check.matches_required_acl('foo.bar.toto'))
        assert_that(acl_check.matches_required_acl('foo.bar.toto.tata'))
        assert_that(acl_check.matches_required_acl('other.bar.toto'), equal_to(False))

    def test_matches_required_acls_when_user_acl_has_not_special_character(self):
        acls = ['foo.bar.toto']
        acl_check = ACLCheck(self.user_uuid, acls)

        assert_that(acl_check.matches_required_acl('foo.bar.toto'))
        assert_that(
            acl_check.matches_required_acl('foo.bar.toto.tata'), equal_to(False)
        )
        assert_that(acl_check.matches_required_acl('other.bar.toto'), equal_to(False))

    def test_matches_required_acls_when_user_acl_has_asterisks(self):
        acls = ['foo.*.*']
        acl_check = ACLCheck(self.user_uuid, acls)

        assert_that(acl_check.matches_required_acl('foo.bar.toto'))
        assert_that(
            acl_check.matches_required_acl('foo.bar.toto.tata'), equal_to(False)
        )
        assert_that(acl_check.matches_required_acl('other.bar.toto'), equal_to(False))

    def test_matches_required_acls_with_multiple_acls(self):
        acls = ['foo', 'foo.bar.toto', 'other.#']
        acl_check = ACLCheck(self.user_uuid, acls)

        assert_that(acl_check.matches_required_acl('foo'))
        assert_that(acl_check.matches_required_acl('foo.bar'), equal_to(False))
        assert_that(acl_check.matches_required_acl('foo.bar.toto'))
        assert_that(
            acl_check.matches_required_acl('foo.bar.toto.tata'), equal_to(False)
        )
        assert_that(acl_check.matches_required_acl('other.bar.toto'))

    def test_matches_required_acls_when_user_acl_has_hashtag_in_middle(self):
        acls = ['foo.bar.#.titi']
        acl_check = ACLCheck(self.user_uuid, acls)

        assert_that(acl_check.matches_required_acl('foo.bar'), equal_to(False))
        assert_that(acl_check.matches_required_acl('foo.bar.toto'), equal_to(False))
        assert_that(
            acl_check.matches_required_acl('foo.bar.toto.tata'), equal_to(False)
        )
        assert_that(acl_check.matches_required_acl('foo.bar.toto.tata.titi'))

    def test_matches_required_acls_when_user_acl_ends_with_me(self):
        acls = ['foo.#.me']
        acl_check = ACLCheck(self.user_uuid, acls)

        assert_that(acl_check.matches_required_acl('foo.bar'), equal_to(False))
        assert_that(acl_check.matches_required_acl('foo.bar.123'))
        assert_that(acl_check.matches_required_acl('foo.bar.toto.123'))
        assert_that(
            acl_check.matches_required_acl('foo.bar.toto.123.titi'), equal_to(False)
        )

    def test_matches_required_acls_when_user_acl_has_me_in_middle(self):
        acls = ['foo.#.me.bar']
        acl_check = ACLCheck(self.user_uuid, acls)

        assert_that(acl_check.matches_required_acl('foo.bar.me.bar'), equal_to(False))
        assert_that(acl_check.matches_required_acl('foo.bar.123'), equal_to(False))
        assert_that(acl_check.matches_required_acl('foo.bar.123.bar'))
        assert_that(acl_check.matches_required_acl('foo.bar.toto.123.bar'))
