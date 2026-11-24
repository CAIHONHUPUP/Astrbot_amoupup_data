# -*- coding: utf-8 -*-
"""跨平台归一单测（从 repo 的 test_cross_platform.py 迁到新布局）。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import Stack  # noqa: E402
from utils.text_process import normalize_platform  # noqa: E402

BOT = "bot1"
QQ_UID = "302578963"
LARK_UID = "ou_cec66"
QQ_UMO = "大肥鱼:FriendMessage:" + QQ_UID
LARK_UMO = "lark:FriendMessage:" + LARK_UID


class PlatformNameTests(unittest.TestCase):
    def test_bot_name_prefix_is_aiocqhttp(self):
        # QQ 的 umo 前缀是机器人名字，不能当平台名
        self.assertEqual(normalize_platform("", QQ_UMO), "aiocqhttp")
        self.assertEqual(normalize_platform("大肥鱼", QQ_UMO), "aiocqhttp")

    def test_known_platforms_and_aliases(self):
        self.assertEqual(normalize_platform("qq", ""), "aiocqhttp")
        self.assertEqual(normalize_platform("feishu", ""), "lark")
        self.assertEqual(normalize_platform("", LARK_UMO), "lark")
        self.assertEqual(normalize_platform("webchat", ""), "webchat")


class CrossPlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-cross-")
        self.stack = Stack(self.tmp)
        s = self.stack
        a = s.persons.resolve_person(BOT, "aiocqhttp", QQ_UID, "family")
        b = s.persons.resolve_person(BOT, "lark", LARK_UID, "family")
        if a != b:
            s.persons.merge_persons(BOT, a, b)
        self.base = 1_800_000_000

    def tearDown(self):
        self.stack.close()

    def test_her_reply_gets_person_id_and_shows_in_context(self):
        s = self.stack
        s.messages.add_message(BOT, QQ_UMO, QQ_UID, "family", "user", "我今天吃面了", created_at=self.base)
        bot_mid = s.messages.add_message(BOT, QQ_UMO, "bot", "大肥鱼", "bot", "吃面好呀",
                                         created_at=self.base + 5)
        row = s.messages.conn.execute("SELECT pid FROM messages WHERE id=?", (bot_mid,)).fetchone()
        self.assertGreater(int(row["pid"] or 0), 0, "她的回复要挂到这个人的 pid 上")
        ctx = s.persons.person_context(BOT, "lark", LARK_UID, 8)
        roles = {r["role"] for r in ctx.get("recent") or []}
        self.assertIn("bot", roles, "跨平台上下文要能看到她说过的话")

    def test_memories_visible_from_both_identities(self):
        s = self.stack
        s.memory.remember(BOT, QQ_UID, "他今天中午吃了牛肉面", importance=0.7)
        qq_ctx = s.persons.person_context(BOT, "aiocqhttp", QQ_UID, 5)
        lark_ctx = s.persons.person_context(BOT, "lark", LARK_UID, 5)
        self.assertTrue(qq_ctx.get("memories"), "QQ 侧要能读到记忆")
        self.assertTrue(lark_ctx.get("memories"), "飞书侧也要能读到同一份记忆")

    def test_messages_since_by_conversation_keeps_both_sides(self):
        s = self.stack
        s.messages.add_message(BOT, QQ_UMO, QQ_UID, "family", "user", "在吗", created_at=self.base)
        s.messages.add_message(BOT, QQ_UMO, "bot", "大肥鱼", "bot", "在的", created_at=self.base + 5)
        rows = s.messages.messages_since(BOT, "", self.base - 1, 50, umo=QQ_UMO)
        self.assertEqual({r["role"] for r in rows}, {"user", "bot"})
        only_user = s.messages.messages_since(BOT, QQ_UID, self.base - 1, 50)
        self.assertEqual({r["role"] for r in only_user}, {"user"})


if __name__ == "__main__":
    unittest.main()
