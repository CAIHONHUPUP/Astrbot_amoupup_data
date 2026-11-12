# -*- coding: utf-8 -*-
"""身份归一：QQ / 飞书同一个人写消息和记忆时都落到主 uid。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from store import DataStore  # noqa: E402

BOT = "大肥鱼"
QQ = "302578963"
LARK = "ou_cec66ea605ad17f62632fb044bc5f6ac"


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-id-")
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"))
        self.pid = self.store.link_identity(BOT, "aiocqhttp", QQ, 0, "family")
        self.store.link_identity(BOT, "lark", LARK, self.pid, "family")

    def tearDown(self):
        self.store.close()

    def test_message_uses_canonical_uid(self):
        self.store.add_message(BOT, "lark:FriendMessage:" + LARK, LARK, "family",
                               "user", "飞书说中午喝汤", platform="lark")
        rows = self.store.list_messages(BOT, limit=5)
        self.assertEqual(str(rows[0]["uid"]), LARK)

    def test_memory_uses_canonical_uid(self):
        self.store.remember(BOT, LARK, "主人中午喝番茄蘑菇汤。")
        rows = self.store.list_memories(BOT, uid=QQ)
        self.assertTrue(rows)
        self.assertEqual(str(rows[0]["uid"]), LARK)

    def test_normalize_old_rows(self):
        self.store.add_message(BOT, "lark:FriendMessage:" + LARK, LARK, "family",
                               "user", "旧飞书消息", platform="lark")
        # 模拟历史数据：直接改成 open_id
        with self.store.lock:
            self.store.conn.execute("UPDATE messages SET uid=? WHERE bot_id=?", (QQ, BOT))
            self.store.conn.commit()
        changed = self.store.normalize_identities(BOT)
        self.assertGreaterEqual(changed, 1)
        rows = self.store.list_messages(BOT, limit=5)
        self.assertEqual(str(rows[0]["uid"]), LARK)

    def test_recall_from_qq_finds_feishu_memory(self):
        self.store.remember(BOT, QQ, "主人中午喝番茄蘑菇汤。")
        rows = self.store.recall(BOT, QQ, "番茄蘑菇汤")
        self.assertTrue(rows)
        self.assertTrue(any("番茄" in str(row.get("content") or "") for row in rows))
        stored = self.store.list_memories(BOT, uid=LARK)
        self.assertTrue(any("番茄" in str(row.get("content") or "") for row in stored))

    def test_person_context_reads_both_platforms(self):
        self.store.add_message(BOT, "lark:FriendMessage:" + LARK, LARK, "family",
                               "user", "飞书说的内容", platform="lark")
        self.store.add_message(BOT, "大肥鱼:FriendMessage:" + QQ, QQ, "family",
                               "user", "QQ说的内容", platform="aiocqhttp")
        detail = self.store.person_context(BOT, "aiocqhttp", QQ, limit=10)
        texts = [row["content"] for row in detail.get("recent") or []]
        self.assertIn("飞书说的内容", texts)
        self.assertIn("QQ说的内容", texts)


if __name__ == "__main__":
    unittest.main()
