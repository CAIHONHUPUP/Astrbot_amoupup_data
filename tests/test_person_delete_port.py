# -*- coding: utf-8 -*-
"""删除人物：级联清聊天/记忆/画像/关系，失败整笔回滚（2026-11-24）。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import Stack  # noqa: E402


class PersonDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-del-")
        self.stack = Stack(self.tmp, "amo_data.db")
        self.bot = "bot1"
        self.conn = self.stack.db.conn
        self.pid = self.stack.persons.resolve_person(self.bot, "lark", "ou_abc", "小明")
        now = 1795519000
        self.conn.execute(
            "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, platform, pid) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (self.bot, "lark:FriendMessage:ou_abc", "ou_abc", "小明", "user", "在吗", now, "lark", self.pid))
        self.conn.execute(
            "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, platform, pid) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (self.bot, "lark:FriendMessage:ou_abc", "ou_abc", "她", "bot", "在的", now, "lark", self.pid))
        cur = self.conn.execute(
            "INSERT INTO memories(bot_id, uid, kind, content, importance, created_at, last_hit_at, pid, platform) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (self.bot, "ou_abc", "fact", "他明天出差", 0.6, now, 0, self.pid, "lark"))
        mem_id = int(cur.lastrowid)
        self.conn.execute(
            "INSERT INTO memory_vec(memory_id, model, dim, vec, updated_at) VALUES(?,?,?,?,?)",
            (mem_id, "m", 2, b"\x00\x01", now))
        self.conn.execute(
            "INSERT INTO profiles(bot_id, uid, name, relation, updated_at) VALUES(?,?,?,?,?)",
            (self.bot, "ou_abc", "小明", "朋友", now))
        self.conn.execute(
            "INSERT INTO relations(bot_id, a_uid, b_uid, relation, note, updated_at) VALUES(?,?,?,?,?,?)",
            (self.bot, "ou_abc", "bot", "朋友", "", now))
        self.conn.commit()

    def tearDown(self):
        self.stack.close()

    def test_dry_run_counts_without_deleting(self):
        out = self.stack.persons.delete_person(self.bot, self.pid, dry_run=True)
        self.assertTrue(out["ok"], out)
        counts = out["counts"]
        self.assertEqual(counts["messages"], 2)
        self.assertEqual(counts["memories"], 1)
        self.assertEqual(counts["profiles"], 1)
        self.assertEqual(counts["links"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE bot_id=? AND pid=?", (self.bot, self.pid)).fetchone()[0], 2)

    def test_delete_cascades_everything(self):
        out = self.stack.persons.delete_person(self.bot, self.pid)
        self.assertTrue(out["ok"], out)
        for table, where, args in (
            ("messages", "bot_id=? AND pid=?", (self.bot, self.pid)),
            ("memories", "bot_id=? AND pid=?", (self.bot, self.pid)),
            ("profiles", "bot_id=? AND uid=?", (self.bot, "ou_abc")),
            ("relations", "bot_id=? AND a_uid=?", (self.bot, "ou_abc")),
            ("person_links", "bot_id=? AND pid=?", (self.bot, self.pid)),
            ("persons", "bot_id=? AND pid=?", (self.bot, self.pid)),
        ):
            left = self.conn.execute("SELECT COUNT(*) FROM " + table + " WHERE " + where, args).fetchone()[0]
            self.assertEqual(left, 0, table)
        # 向量表跟着记忆一起清
        left_vec = self.conn.execute("SELECT COUNT(*) FROM memory_vec").fetchone()[0]
        self.assertEqual(left_vec, 0)

    def test_missing_person_is_reported(self):
        out = self.stack.persons.delete_person(self.bot, 99999)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "not_found")


if __name__ == "__main__":
    unittest.main()
