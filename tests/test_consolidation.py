# -*- coding: utf-8 -*-
"""旧流水巩固：同天同会话流水合并、原文不丢、合并后不进召回。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import consolidation  # noqa: E402
from support.store_fixture import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


def _row(rid, content, days=10, kind="activity", importance=0.3):
    now = int(time.time())
    return {"id": rid, "uid": UID, "scope": "private", "group_id": "", "kind": kind,
            "importance": importance, "created_at": now - days * 86400,
            "last_hit_at": now - days * 86400, "content": content}


class PlanTests(unittest.TestCase):
    def test_groups_same_day_noise(self):
        now = int(time.time())
        rows = [_row(1, "喝了咖啡", 10), _row(2, "写了代码", 10), _row(3, "看了球赛", 10)]
        groups = consolidation.plan(rows, now, days=7, min_items=3)
        self.assertEqual(len(groups), 1)
        self.assertIn("流水：", groups[0]["content"])
        self.assertIn("喝了咖啡", groups[0]["content"])
        self.assertEqual(sorted(groups[0]["ids"]), [1, 2, 3])

    def test_recent_or_important_untouched(self):
        now = int(time.time())
        rows = [_row(1, "刚刚的事", 0), _row(2, "重要的事", 10, importance=0.9),
                _row(3, "又一件", 10), _row(4, "第三件", 10)]
        self.assertEqual(consolidation.plan(rows, now, days=7, min_items=3), [])

    def test_below_min_items_untouched(self):
        now = int(time.time())
        self.assertEqual(consolidation.plan([_row(1, "a", 10), _row(2, "b", 10)],
                                            now, days=7, min_items=3), [])


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"), settings={
            "embedding_enabled": False, "auto_dedupe": False,
            "recall_entity_graph": False, "recall_entity_tag": False,
        })
        self.ids = [self.store.remember(BOT, UID, text, kind="activity",
                                        importance=0.3)["memory_id"]
                    for text in ("下午喝了杯咖啡", "晚上写了两小时代码", "睡前看了半场球赛")]
        old = int(time.time()) - 10 * 86400
        with self.store.lock:
            for memory_id in self.ids:
                self.store.conn.execute(
                    "UPDATE memories SET created_at=?, last_hit_at=? WHERE id=?",
                    (old, old, memory_id))
            self.store.conn.commit()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _status(self, memory_id):
        row = self.store.conn.execute(
            "SELECT status FROM memories WHERE id=?", (memory_id,)).fetchone()
        return str(row["status"])

    def test_consolidates_into_one(self):
        out = self.store.compression.consolidate_noise(BOT, apply=True)
        self.assertGreaterEqual(out["groups"], 1)
        active = [i for i in self.ids if self._status(i) == "active"]
        merged = [i for i in self.ids if self._status(i) == "merged"]
        self.assertEqual(len(active), 1)
        self.assertEqual(len(merged), 2)
        row = self.store.conn.execute(
            "SELECT content FROM memories WHERE id=?", (active[0],)).fetchone()
        for text in ("咖啡", "代码", "球赛"):
            self.assertIn(text, str(row["content"]), "巩固不能丢原文")

    def test_merged_not_recalled(self):
        self.store.compression.consolidate_noise(BOT, apply=True)
        rows = self.store.recall(BOT, UID, "咖啡")
        self.assertTrue(all(self._status(int(r["id"])) == "active" for r in rows))


if __name__ == "__main__":
    unittest.main()
