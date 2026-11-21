# -*- coding: utf-8 -*-
"""2026-11-18 召回升级：全库向量候选层 / RRF / 拆词 OR 粗筛。

三条要守住的行为：
1. 词面完全重合不了、又不在「最近活跃」池里的记忆，靠向量候选还能被想起来；
2. 关掉这一路（recall_vector_candidates=0）就回到旧行为（反向验证）；
3. 整句 LIKE 命中不了时，拆词 OR 能把「猫 / 团子」这种跨词序的捞回来，关掉就捞不回。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import recall_index  # noqa: E402
from support.store_fixture import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class RrfTests(unittest.TestCase):
    def test_fuses_by_rank_not_raw_score(self):
        # 两路：第一路 1>2，第二路 2>3 → 2 同时出现在两路最前，应该排第一
        self.assertEqual(recall_index.rrf([[1, 2], [2, 3]]), [2, 1, 3])

    def test_ignores_unknown_ids_and_empty_routes(self):
        self.assertEqual(recall_index.rrf([[], ["x", 5]]), [5])
        self.assertEqual(recall_index.rrf([]), [])

    def test_tie_is_deterministic(self):
        self.assertEqual(recall_index.rrf([[3, 4], [4, 3]]), [3, 4])


class VectorIndexTests(unittest.TestCase):
    def _index(self, vecs):
        state = (len(vecs), max(vecs) if vecs else 0, 0)
        return recall_index.VectorIndex(lambda: (state, dict(vecs)))

    def test_search_orders_by_cosine(self):
        index = self._index({1: [1.0, 0.0], 2: [0.0, 1.0], 3: [0.9, 0.1]})
        hits = index.search([1.0, 0.0], 2)
        self.assertEqual([mid for mid, _ in hits], [1, 3])
        self.assertAlmostEqual(hits[0][1], 1.0, places=5)

    def test_dimension_mismatch_returns_empty(self):
        index = self._index({1: [1.0, 0.0]})
        self.assertEqual(index.search([1.0, 0.0, 0.0], 5), [])

    def test_rebuilds_when_state_changes(self):
        state = {"n": 1}
        vecs = {1: [1.0, 0.0]}
        index = recall_index.VectorIndex(lambda: ((state["n"], 1, 0), dict(vecs)))
        self.assertEqual(len(index), 1)
        vecs[2] = [0.0, 1.0]
        state["n"] = 2
        self.assertEqual(len(index), 2, "向量表变了必须重建")

    def test_cosines_only_requested_ids(self):
        index = self._index({1: [1.0, 0.0], 2: [0.0, 1.0]})
        found = index.cosines([1.0, 0.0], [1, 2, 99])
        self.assertEqual(sorted(found), [1, 2])
        self.assertAlmostEqual(found[1], 1.0, places=5)


class CandidateCoverageTests(unittest.TestCase):
    """词面不重合 + 不在最近池 → 只有向量候选这一路能救回来。"""

    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"), settings={
            "embedding_enabled": False, "embedding_backfill": False,
            "recall_recent_pool": 2, "recall_vector_candidates": 10,
            "auto_dedupe": False,  # 流水账内容相近，别让去重把 12 条并成 1 条
        })
        # 先写猫（最老），再用 12 条新流水账把它挤出「最近活跃」池
        self.cat_id = self.store.remember(
            BOT, UID, "主人养了一只橘色的猫，叫团子", importance=0.6)["memory_id"]
        for index in range(12):
            self.store.remember(BOT, UID, "2026-11-%02d 第 %d 条流水：%s"
                                % (index % 5 + 1, index, ("吃了面条", "坐了地铁", "开了会",
                                                          "写了代码", "看了球赛", "买了牛奶")[index % 6]),
                                importance=0.3)
        # 同一秒内写入会撞时间戳：显式把猫排到最老，others 按顺序变新
        now = int(time.time())
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE memories SET created_at=?, last_hit_at=? WHERE id=?",
                (now - 5000, now - 5000, self.cat_id),
            )
            others = self.store.conn.execute(
                "SELECT id FROM memories WHERE id<>? ORDER BY id", (self.cat_id,)
            ).fetchall()
            for offset, row in enumerate(others):
                self.store.conn.execute(
                    "UPDATE memories SET created_at=?, last_hit_at=? WHERE id=?",
                    (now - 100 + offset, now - 100 + offset, int(row["id"])),
                )
            self.store.conn.commit()
        # 手工写向量：查询「毛孩子」≈ 猫；流水账正交
        self.store._store_vec(self.cat_id, [0.98, 0.2, 0.0])
        for row in self.store.memory.list_memories(BOT, UID, 50):
            if int(row["id"]) != int(self.cat_id):
                self.store._store_vec(int(row["id"]), [0.0, 1.0, 0.0])

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _enable_semantic(self, **extra):
        self.store.settings.update({
            "embedding_enabled": True, "recall_vec_gate": "rank",
            "recall_vec_gate_floor": 0.6, "recall_vec_gate_window": 0.12,
        })
        self.store.settings.update(extra)
        self.store._embed = lambda text: [1.0, 0.0, 0.0] if "毛孩子" in text else [0.0, 1.0, 0.0]
        self.store.memory._emb_cache.clear()

    def test_vector_route_finds_memory_outside_recent_pool(self):
        self._enable_semantic()
        rows = self.store.recall(BOT, UID, "毛孩子叫啥来的")
        self.assertEqual([row["id"] for row in rows], [self.cat_id],
                         "最近池只有 2 条、词面又对不上，只有向量候选能想起来")

    def test_disabling_vector_route_reverts_behaviour(self):
        self.store.settings["recall_recent_pool"] = 2
        self._enable_semantic(recall_vector_candidates=0)
        rows = self.store.recall(BOT, UID, "毛孩子叫啥来的")
        self.assertEqual(rows, [], "关掉向量候选就该回到「想不起来」的旧行为")

    def test_token_or_matches_across_word_order(self):
        self.store.settings["recall_recent_pool"] = 2
        rows = self.store.recall(BOT, UID, "你还记得团子吗")
        self.assertTrue(rows and "团子" in rows[0]["content"])

    def test_disabling_token_or_reverts_behaviour(self):
        self.store.settings["recall_token_or"] = False
        self.store.settings["recall_recent_pool"] = 2
        # 实体图谱也能按名字捞回来，这里只测「拆词」这一路，把它一起关掉
        self.store.settings["recall_entity_graph"] = False
        self.store.settings["recall_graph_route"] = False
        rows = self.store.recall(BOT, UID, "你还记得团子吗")
        self.assertEqual(rows, [], "关掉拆词粗筛后整句 LIKE 命中不了，应当空召回")


if __name__ == "__main__":
    unittest.main()
