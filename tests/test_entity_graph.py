# -*- coding: utf-8 -*-
"""通用实体图谱：抽取 / 连边 / 实体路 + 一跳共现召回。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import entity_graph  # noqa: E402
from support.store_fixture import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class ExtractTests(unittest.TestCase):
    def test_quoted_entity(self):
        self.assertIn("围巾", entity_graph.extract_entities("他送了我一条「围巾」"))

    def test_known_name_and_stopwords(self):
        names = entity_graph.extract_entities("小周最近怎么样", ["小周"])
        self.assertIn("小周", names)
        self.assertNotIn("最近", names)

    def test_cap(self):
        text = "「甲一」「乙二」「丙三」「丁四」「戊五」「己六」「庚七」"
        self.assertLessEqual(len(entity_graph.extract_entities(text)), entity_graph.MAX_ENTITIES)

    def test_empty(self):
        self.assertEqual(entity_graph.extract_entities(""), [])


class GraphStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"), settings={
            "embedding_enabled": False, "auto_dedupe": False,
            "recall_entity_graph": True,
            "recall_vector_candidates": 0, "recall_graph_route": False,
        })

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_link_memory_is_idempotent(self):
        mid = self.store.remember(BOT, UID, "「小周」送了我一条「围巾」")["memory_id"]
        before = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM entity_edges").fetchone()["n"]
        entity_graph.link_memory(self.store.memory.dao, BOT, mid, "「小周」送了我一条「围巾」", ["小周"])
        after = self.store.conn.execute(
            "SELECT COUNT(*) AS n FROM entity_edges").fetchone()["n"]
        self.assertEqual(before, after, "重复连边不能涨边数")

    def test_graph_finds_neighbor_memory(self):
        # A：围巾 + 小周；B：只有小周（正文里没有「围巾」）
        a_id = self.store.remember(BOT, UID, "「小周」送了我一条「围巾」")["memory_id"]
        b_id = self.store.remember(BOT, UID, "「小周」最近换了工作")["memory_id"]
        ids = [int(row["id"]) for row in self.store.recall(BOT, UID, "围巾")]
        self.assertIn(a_id, ids)
        self.assertIn(b_id, ids, "围巾 → 共现的小周 → 带出另一条记忆")

    def test_graph_off_reverts(self):
        self.store.settings["recall_entity_graph"] = False
        self.store.remember(BOT, UID, "「小周」送了我一条「围巾」")
        b_id = self.store.remember(BOT, UID, "「小周」最近换了工作")["memory_id"]
        ids = [int(row["id"]) for row in self.store.recall(BOT, UID, "围巾")]
        self.assertNotIn(b_id, ids, "关掉图谱就不能靠共现带出邻居")


if __name__ == "__main__":
    unittest.main()
