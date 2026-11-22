# -*- coding: utf-8 -*-
"""能记面板指标口径（task-27）：聊天只算真实对话、摘要单列。

生产事实：面板「聊天 436」= user 104 + bot 186 + summary 146；真实聊天 290，
高估 33.5%。修复后 maintenance.stats 同时给 messages（全表，兼容）、
messages_chat（role in user/bot）、messages_summary（role=summary）。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
for path in (ROOT, TESTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import fixtures  # noqa: E402

BOT = "aiocqhttp"
UMO = "aiocqhttp:FriendMessage:10001"


class PanelChatMetricsTests(unittest.TestCase):
    def test_chat_and_summary_are_separate(self):
        with fixtures.TempStore() as store:
            store.messages.add_message(BOT, UMO, "10001", "他", role="user", content="在吗")
            store.messages.add_message(BOT, UMO, "10001", "他", role="bot", content="在的")
            # 摘要行由压缩引擎直接写 role=summary，add_message 会把 role 收敛成 user/bot；
            # 这里按生产形态直接补一行，验证面板口径而不是 add_message 语义。
            with store.lock:
                store.conn.execute(
                    "INSERT INTO messages (umo, uid, name, role, content, created_at, platform, bot_id) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (UMO, "10001", "他", "summary", "摘要", 1795317000, "aiocqhttp", BOT))
                store.conn.commit()
            counts = store.maintenance.stats(BOT)
            self.assertEqual(counts["messages"], 3)
            self.assertEqual(counts["messages_chat"], 2)
            self.assertEqual(counts["messages_summary"], 1)

    def test_chat_count_never_exceeds_total(self):
        with fixtures.TempStore() as store:
            counts = store.maintenance.stats(BOT)
            self.assertEqual(counts["messages"], 0)
            self.assertEqual(counts["messages_chat"], 0)
            self.assertEqual(counts["messages_summary"], 0)
            self.assertLessEqual(counts["messages_chat"], counts["messages"])

    def test_bot_filter_applies_to_split_counts(self):
        with fixtures.TempStore() as store:
            store.messages.add_message(BOT, UMO, "10001", "他", role="user", content="a")
            store.messages.add_message("lark", "lark:FriendMessage:ou_x", "ou_x", "他",
                                       role="user", content="b")
            counts = store.maintenance.stats(BOT)
            self.assertEqual(counts["messages_chat"], 1)
            self.assertEqual(counts["messages_summary"], 0)


if __name__ == "__main__":
    unittest.main()
