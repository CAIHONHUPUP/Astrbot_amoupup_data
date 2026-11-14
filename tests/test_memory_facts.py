# -*- coding: utf-8 -*-
"""事实级去重：同义偏好只留一条、流水账不去重、细节合并、状态覆盖、检索去重、/遗忘。"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from store import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"


class MemoryFactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "m.db"))

    def tearDown(self):
        self.store.close()

    def _rows(self, like="%螺蛳粉%", active=True):
        sql = ("SELECT id, content, kind, importance, merged_count, status "
               "FROM memories WHERE bot_id=? AND content LIKE ?")
        params = [BOT, like]
        if active:
            sql += " AND COALESCE(status,'active')='active'"
        return self.store.conn.execute(sql, params).fetchall()

    def _insert(self, content, kind="fact", importance=0.5, idem=""):
        now = int(time.time())
        return self.store.memory.dao.insert(
            BOT, UID, 0, "", "private", "", kind, "user", content,
            importance, 0, now, now, idem)

    def test_three_synonyms_leave_one_high_weight_memory(self):
        for text in ("我喜欢吃螺蛳粉", "螺蛳粉真好吃", "我最爱吃螺蛳粉"):
            self.store.remember(BOT, UID, text, kind="fact", importance=0.5)
        rows = self._rows()
        self.assertEqual(len(rows), 1, [dict(r) for r in self._rows(active=False)])
        self.assertGreaterEqual(float(rows[0]["importance"]), 0.56)
        self.assertGreaterEqual(int(rows[0]["merged_count"]), 2)

    def test_transactions_are_never_deduped(self):
        self.store.remember(BOT, UID, "今天买奶茶花了15元", kind="expense")
        self.store.remember(BOT, UID, "今天买奶茶花了15元", kind="expense")
        rows = self._rows("%奶茶%")
        self.assertEqual(len(rows), 2)

    def test_detail_is_merged_not_overwritten(self):
        self.store.remember(BOT, UID, "老公喜欢吃螺蛳粉", kind="fact")
        self.store.remember(BOT, UID, "老公喜欢吃加辣的螺蛳粉", kind="fact")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        text = str(rows[0]["content"])
        self.assertIn("螺蛳粉", text)
        self.assertIn("加辣", text)

    def test_state_reversal_expires_old(self):
        old_id = self.store.remember(BOT, UID, "我喜欢喝冰美式", kind="fact")["memory_id"]
        self.store.remember(BOT, UID, "我最近胃不好，不喝冰的了，改喝热牛奶", kind="fact")
        row = self.store.conn.execute("SELECT status FROM memories WHERE id=?", (old_id,)).fetchone()
        self.assertNotEqual(str(row["status"] or "active"), "active")
        active = [r for r in self._rows("%冰美式%") if str(r["status"] or "") == "active"]
        self.assertEqual(active, [])

    def test_compression_merges_same_fact(self):
        self._insert("老公喜欢吃螺蛳粉")
        self._insert("老公喜欢吃加辣的螺蛳粉")
        info = self.store.compression.compress_facts(BOT)
        self.assertGreaterEqual(info["merged"], 1)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("加辣", str(rows[0]["content"]))
        self.assertGreaterEqual(int(rows[0]["merged_count"]), 1)

    def test_recall_dedupes_same_fact(self):
        self._insert("我喜欢吃螺蛳粉")
        self._insert("螺蛳粉真好吃")
        items = self.store.recall(BOT, UID, "螺蛳粉", limit=5)
        hit = [item for item in items if "螺蛳粉" in str(item.get("content") or "")]
        self.assertEqual(len(hit), 1, hit)

    def test_forget_physically_deletes(self):
        for text in ("我喜欢吃螺蛳粉", "螺蛳粉真好吃", "昨天聊到螺蛳粉"):
            self._insert(text)
        count = self.store.memory.forget(BOT, "螺蛳粉")
        self.assertEqual(count, 3)
        rows = self._rows(active=False)
        self.assertEqual(len(rows), 0)


if __name__ == "__main__":
    unittest.main()
