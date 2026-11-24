# -*- coding: utf-8 -*-
"""跨平台人物上下文单测（从 repo 的 test_person_context.py 迁到新布局）。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import Stack  # noqa: E402

BOT = "bot1"
QQ_UID = "302578963"
LARK_UID = "ou_cec66"


class PersonContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-pctx-")
        self.stack = Stack(self.tmp)
        s = self.stack
        s.profiles.save_profile(BOT, QQ_UID, name="family")
        s.profiles.save_profile(BOT, LARK_UID, name="family")
        a = s.persons.resolve_person(BOT, "aiocqhttp", QQ_UID, "family")
        b = s.persons.resolve_person(BOT, "lark", LARK_UID, "family")
        if a != b:
            s.persons.merge_persons(BOT, a, b)

    def tearDown(self):
        self.stack.close()

    def _add(self, platform, uid, text, ts):
        umo = ("aiocqhttp" if platform == "aiocqhttp" else platform) + ":FriendMessage:" + uid
        self.stack.messages.add_message(BOT, umo, uid, "他", "user", text, created_at=ts)

    def test_each_platform_is_represented(self):
        base = 1_800_000_000
        for i in range(10):
            self._add("aiocqhttp", QQ_UID, "QQ第%d句" % i, base + i)
        self._add("lark", LARK_UID, "飞书唯一一句", base - 100)
        ctx = self.stack.persons.person_context(BOT, "aiocqhttp", QQ_UID, 6)
        platforms = {m["platform"] for m in ctx.get("recent") or []}
        self.assertEqual(platforms, {"aiocqhttp", "lark"})
        self.assertTrue(any("飞书唯一一句" in m["content"] for m in ctx["recent"]))
        self.assertEqual([o["platform"] for o in ctx.get("others") or []], ["lark"])

    def test_same_uid_on_other_platform_merges_automatically(self):
        s = self.stack
        self._add("aiocqhttp", QQ_UID, "QQ上说了一句", 1_800_000_000)
        s.messages.add_message(BOT, "lark:FriendMessage:" + QQ_UID, QQ_UID, "他", "user",
                               "飞书上说了一句", created_at=1_800_000_100)
        pid_qq = s.persons.resolve_person(BOT, "aiocqhttp", QQ_UID)
        pid_lark = s.persons.resolve_person(BOT, "lark", QQ_UID)
        self.assertEqual(pid_qq, pid_lark)
        names = [p["name"] for p in s.persons.list_persons(BOT)]
        self.assertEqual(len(names), 1)

    def test_latest_lark_message_with_canonical_uid_is_seen(self):
        base = 1_800_000_000
        self._add("lark", LARK_UID, "飞书旧消息", base)
        self._add("lark", QQ_UID, "飞书最新消息", base + 100)
        ctx = self.stack.persons.person_context(BOT, "lark", LARK_UID, 6)
        texts = [m["content"] for m in ctx.get("recent") or []]
        self.assertIn("飞书最新消息", texts)

    def test_unknown_person_returns_empty(self):
        self.assertEqual(self.stack.persons.person_context(BOT, "aiocqhttp", "no-such-uid", 6), {})


if __name__ == "__main__":
    unittest.main()
