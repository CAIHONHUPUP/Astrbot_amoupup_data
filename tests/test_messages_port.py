# -*- coding: utf-8 -*-
"""messages_between / messages_since 单测（从 repo 两个文件合并迁到新布局）。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import Stack  # noqa: E402

BOT = "大肥鱼"
UID = "302578963"


class MessagesBetweenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-msgs-")
        self.stack = Stack(self.tmp, "a.db")
        for i in range(5):
            self.stack.messages.add_message(BOT, "u", UID, "他", "user", "第%d条" % i,
                                            created_at=1000 + i * 10)

    def tearDown(self):
        self.stack.close()

    def test_range_filters(self):
        rows = self.stack.messages.messages_between(BOT, 1010, 1030)
        self.assertEqual([r["content"] for r in rows], ["第1条", "第2条"])

    def test_uid_filter(self):
        self.stack.messages.add_message(BOT, "b", "470488586", "她", "bot", "她的回复",
                                        created_at=1015)
        rows = self.stack.messages.messages_between(BOT, 1000, 1100, uid=UID)
        self.assertEqual(len(rows), 5)
        rows_all = self.stack.messages.messages_between(BOT, 1000, 1100)
        self.assertEqual(len(rows_all), 6)


class MessagesSinceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-since-")
        self.stack = Stack(self.tmp, "a.db")

    def tearDown(self):
        self.stack.close()

    def _add(self, role, text, ts):
        self.stack.messages.add_message(
            BOT, "aiocqhttp:FriendMessage:" + UID, UID, "family",
            "user" if role == "user" else "bot", text, created_at=ts)

    def test_filters_by_time_and_orders_ascending(self):
        base = 1_800_000_000
        self._add("user", "旧消息", base)
        self._add("bot", "第二条", base + 60)
        self._add("user", "新消息", base + 120)
        rows = self.stack.messages.messages_since(BOT, UID, since_ts=base + 30)
        self.assertEqual([r["content"] for r in rows], ["第二条", "新消息"])
        self.assertEqual([r["role"] for r in rows], ["bot", "user"])

    def test_zero_since_returns_from_start(self):
        base = 1_800_000_000
        self._add("user", "一", base)
        self._add("user", "二", base + 10)
        rows = self.stack.messages.messages_since(BOT, UID, since_ts=0)
        self.assertEqual(len(rows), 2)

    def test_isolated_by_uid(self):
        base = 1_800_000_000
        self._add("user", "我的", base)
        self.stack.messages.add_message(BOT, "aiocqhttp:FriendMessage:other", "other", "他",
                                        "user", "别人的", created_at=base + 10)
        rows = self.stack.messages.messages_since(BOT, UID)
        self.assertEqual([r["content"] for r in rows], ["我的"])

    def test_limit(self):
        base = 1_800_000_000
        for i in range(5):
            self._add("user", "第%d条" % i, base + i)
        rows = self.stack.messages.messages_since(BOT, UID, limit=3)
        self.assertEqual(len(rows), 3)


if __name__ == "__main__":
    unittest.main()
