# -*- coding: utf-8 -*-
"""身份归一（消息/记忆都落到主 uid）单测，从 repo 的 test_identity.py 迁到新布局。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import Stack  # noqa: E402

BOT = "大肥鱼"
QQ = "302578963"
LARK = "ou_cec66ea605ad17f62632fb044bc5f6ac"


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-id-")
        self.stack = Stack(self.tmp)
        s = self.stack
        self.pid = s.persons.link_identity(BOT, "aiocqhttp", QQ, 0, "family")
        s.persons.link_identity(BOT, "lark", LARK, self.pid, "family")

    def tearDown(self):
        self.stack.close()

    def test_message_uses_canonical_uid(self):
        s = self.stack
        s.messages.add_message(BOT, "lark:FriendMessage:" + LARK, LARK, "family",
                               "user", "飞书说中午喝汤", platform="lark")
        rows = s.messages.list_messages(BOT, limit=5)
        self.assertEqual(str(rows[0]["uid"]), LARK)

    def test_memory_uses_canonical_uid(self):
        s = self.stack
        s.memory.remember(BOT, LARK, "主人中午喝番茄蘑菇汤。")
        rows = s.memory.list_memories(BOT, uid=QQ)
        self.assertTrue(rows)
        self.assertEqual(str(rows[0]["uid"]), LARK)

    def test_normalize_old_rows(self):
        s = self.stack
        s.messages.add_message(BOT, "lark:FriendMessage:" + LARK, LARK, "family",
                               "user", "旧飞书消息", platform="lark")
        with s.messages.lock:
            s.messages.conn.execute("UPDATE messages SET uid=? WHERE bot_id=?", (QQ, BOT))
            s.messages.conn.commit()
        changed = s.persons.normalize_identities(BOT)
        self.assertGreaterEqual(changed, 1)
        rows = s.messages.list_messages(BOT, limit=5)
        self.assertEqual(str(rows[0]["uid"]), LARK)

    def test_recall_from_qq_finds_feishu_memory(self):
        s = self.stack
        s.memory.remember(BOT, QQ, "主人中午喝番茄蘑菇汤。")
        rows = s.retrieval.recall(BOT, QQ, "番茄蘑菇汤")
        self.assertTrue(rows)
        self.assertTrue(any("番茄" in str(row.get("content") or "") for row in rows))
        stored = s.memory.list_memories(BOT, uid=LARK)
        self.assertTrue(any("番茄" in str(row.get("content") or "") for row in stored))

    def test_person_context_reads_both_platforms(self):
        s = self.stack
        s.messages.add_message(BOT, "lark:FriendMessage:" + LARK, LARK, "family",
                               "user", "飞书说的内容", platform="lark")
        s.messages.add_message(BOT, "大肥鱼:FriendMessage:" + QQ, QQ, "family",
                               "user", "QQ说的内容", platform="aiocqhttp")
        detail = s.persons.person_context(BOT, "aiocqhttp", QQ, limit=10)
        texts = [row["content"] for row in detail.get("recent") or []]
        self.assertIn("飞书说的内容", texts)
        self.assertIn("QQ说的内容", texts)


if __name__ == "__main__":
    unittest.main()
