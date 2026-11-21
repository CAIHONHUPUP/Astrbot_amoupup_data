# -*- coding: utf-8 -*-
"""跨天同主题语义巩固：聚类 / 防幻觉校验 / 落库。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import semantic_consolidation as sc  # noqa: E402
from support.store_fixture import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


def _row(rid, content, days=0, kind="fact", scope="private", group_id="", importance=0.6):
    now = int(time.time())
    return {"id": rid, "uid": UID, "scope": scope, "group_id": group_id, "kind": kind,
            "content": content, "importance": importance,
            "created_at": now - days * 86400, "last_hit_at": now - days * 86400}


class ClusterTests(unittest.TestCase):
    def test_same_topic_cross_day(self):
        rows = [_row(1, "主人喜欢喝冰美式", 10), _row(2, "主人爱喝冰美式咖啡", 3)]
        groups = sc.cluster(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["ids"], [1, 2])

    def test_different_scope_not_merged(self):
        rows = [_row(1, "主人喜欢喝冰美式", 10), _row(2, "主人喜欢喝冰美式", 3, scope="group", group_id="g1")]
        self.assertEqual(sc.cluster(rows), [])

    def test_unrelated_not_merged(self):
        rows = [_row(1, "主人喜欢喝冰美式", 10), _row(2, "外面下雨了记得带伞", 3)]
        self.assertEqual(sc.cluster(rows), [])

    def test_proactive_template_not_merged(self):
        rows = [_row(1, "今天主动跟老公分享：早，我在楼下吃粉呢", 3),
                _row(2, "今天主动跟老公分享：老公，还在排队腿都坐麻了", 2)]
        self.assertEqual(sc.cluster(rows), [], "模板开头不能把不同的事并到一起")

    def test_date_prefix_not_merged(self):
        rows = [_row(1, "2026-11-16 11:35 决策：心情好但空仓观望", 2),
                _row(2, "2026-11-16 11:20 决策：买了五百块股票", 1)]
        self.assertEqual(sc.cluster(rows), [])

    def test_non_mergeable_kind_skipped(self):
        rows = [_row(1, "主人喜欢喝冰美式", 10, kind="activity"),
                _row(2, "主人爱喝冰美式咖啡", 3, kind="activity")]
        self.assertEqual(sc.cluster(rows), [])


class AcceptTests(unittest.TestCase):
    def test_accepts_merged_fact(self):
        contents = ["主人喜欢喝冰美式", "主人爱喝冰美式咖啡"]
        self.assertTrue(sc.accept_merged("主人喜欢喝冰美式咖啡", contents))

    def test_rejects_short_or_offtopic(self):
        contents = ["主人喜欢喝冰美式", "主人爱喝冰美式咖啡"]
        self.assertFalse(sc.accept_merged("好", contents))
        self.assertFalse(sc.accept_merged("外面下雨了记得带伞", contents))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"), settings={
            "embedding_enabled": False, "auto_dedupe": False,
            "recall_entity_graph": False, "recall_entity_tag": False,
        })

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_apply_merge(self):
        a = self.store.remember(BOT, UID, "主人喜欢喝冰美式", kind="preference")["memory_id"]
        b = self.store.remember(BOT, UID, "主人爱喝冰美式咖啡", kind="preference")["memory_id"]
        groups = self.store.memory.semantic_clusters(BOT)
        self.assertEqual(len(groups), 1)
        merged = self.store.memory.apply_semantic_merge(groups[0]["keep_id"],
                                                        "主人喜欢喝冰美式咖啡", groups[0]["ids"])
        self.assertEqual(merged, 1)
        statuses = {m: self.store.conn.execute(
            "SELECT status FROM memories WHERE id=?", (m,)).fetchone()["status"] for m in (a, b)}
        self.assertEqual(sorted(statuses.values()), ["active", "merged"])


if __name__ == "__main__":
    unittest.main()
