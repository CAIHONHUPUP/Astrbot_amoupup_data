# -*- coding: utf-8 -*-
"""身份别名单测（从 repo 的 test_identity_alias.py 迁到新布局）。

旧 `store.DataStore` → `ProfileEngine` + `PersonEngine`（共用 _harness.Stack）。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import Stack  # noqa: E402

BOT = "b"
OWNER_QQ = "302578963"
FEISHU = "ou_cec66"


class IdentityAliasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-identity-")
        self.stack = Stack(self.tmp)
        s = self.stack
        s.profiles.save_profile(BOT, OWNER_QQ, name="family")
        s.profiles.save_profile(BOT, FEISHU, name="family")
        self.pid = s.persons.resolve_person(BOT, "aiocqhttp", OWNER_QQ, "family")
        # 规范身份先落地（线上是已经存在的飞书身份），别名再把遗留身份指过来
        s.persons.resolve_person(BOT, "lark", FEISHU, "family")
        s.persons.set_identity_alias(BOT, "lark", OWNER_QQ, "lark", FEISHU)
        s.persons.set_identity_alias(BOT, "qq", OWNER_QQ, "aiocqhttp", OWNER_QQ)

    def tearDown(self):
        self.stack.close()

    def test_legacy_lark_uid_resolves_to_feishu_identity(self):
        pid = self.stack.persons.resolve_person(BOT, "lark", OWNER_QQ)
        self.assertEqual(pid, self.pid)
        detail = self.stack.persons.person_of(BOT, "lark", OWNER_QQ)
        self.assertEqual(int(detail["pid"]), self.pid)
        links = {(l["platform"], l["uid"]) for l in detail.get("links") or []}
        self.assertIn(("lark", FEISHU), links)

    def test_legacy_qq_platform_resolves_to_aiocqhttp(self):
        pid = self.stack.persons.resolve_person(BOT, "qq", OWNER_QQ)
        self.assertEqual(pid, self.pid)
        detail = self.stack.persons.person_of(BOT, "qq", OWNER_QQ)
        self.assertEqual(int(detail["pid"]), self.pid)

    def test_alias_listed(self):
        rows = self.stack.persons.list_identity_aliases(BOT)
        self.assertTrue(any(r["platform"] == "lark" and r["alias_uid"] == FEISHU for r in rows))

    def test_unknown_identity_not_redirected(self):
        self.assertEqual(self.stack.persons.person_of(BOT, "lark", "someone-else"), {})


if __name__ == "__main__":
    unittest.main()
