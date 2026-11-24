# -*- coding: utf-8 -*-
"""召回闸门 / 去重 / 遗忘曲线单测（从 repo 的 test_recall_fixes.py 迁到新布局）。

原文件的 FakeVectorTests 测的是 `store._embed` / `_store_vec` 私有接口，
新布局已换成 MemoryEngine 的 embedding 队列，这里不迁（语义召回由
test_embedding_offline 覆盖）。
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import Stack  # noqa: E402

BOT = "bot1"
UID = "302578963"


class RecallGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-recallfix-")
        self.stack = Stack(self.tmp, "amo_data.db")
        m = self.stack.memory
        m.remember(BOT, UID, "2026-11-06 晚上，主人吃了牛杂和小烧烤，还拍了照片发给我看。", importance=0.7)
        m.remember(BOT, UID, "主人今晚十点多跟我说，以后飞书是主战场，有事都去飞书找他。", importance=0.6)
        m.remember(BOT, UID, "主人养了一只叫团子的猫，橘色的。", importance=0.5)

    def tearDown(self):
        self.stack.close()

    def test_question_shell_still_hits(self):
        """整句问句（amo 实际传的格式）要能命中，这是 2.2.8 的老毛病。"""
        rows = self.stack.retrieval.recall(BOT, UID, "你还记得我周五晚上吃了什么吗")
        self.assertTrue(rows, "整句问句应该命中")
        self.assertIn("牛杂", rows[0]["content"])

    def test_two_char_query_hits(self):
        r = self.stack.retrieval
        self.assertIn("牛杂", r.recall(BOT, UID, "牛杂")[0]["content"])
        self.assertIn("团子", r.recall(BOT, UID, "团子")[0]["content"])

    def test_no_false_positive(self):
        """库里没有相关内容时必须返回空，而不是给一条高分的无关记忆。"""
        r = self.stack.retrieval
        self.assertEqual(r.recall(BOT, UID, "上次说的小挂件呢"), [])
        self.assertEqual(r.recall(BOT, UID, "外面下雨了吗"), [])

    def test_generic_time_word_is_not_evidence(self):
        """「今天」这种泛指不能单独当作命中证据。"""
        self.assertEqual(self.stack.retrieval.recall(BOT, UID, "你今天心情怎么样"), [])

    def test_min_matched_terms_can_be_disabled(self):
        off = Stack(self.tmp, "off.db", settings={"min_matched_terms": 0})
        try:
            off.memory.remember(BOT, UID, "主人养了一只叫团子的猫。", importance=0.5)
            self.assertTrue(off.retrieval.recall(BOT, UID, "上次说的小挂件呢"),
                            "关掉闸之后恢复成「总会给一条」的旧行为")
        finally:
            off.close()

    def test_scope_isolation(self):
        self.stack.memory.remember(BOT, UID, "群里有人说周末去爬山", scope="group", group_id="g1")
        rows = self.stack.retrieval.recall(BOT, UID, "爬山", scope="private")
        self.assertFalse([r for r in rows if "爬山" in r["content"]])


class DedupeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-dedupe-")
        self.stack = Stack(self.tmp, "amo_data.db")

    def tearDown(self):
        self.stack.close()

    def _count(self):
        return int(self.stack.conn.execute(
            "SELECT COUNT(*) AS n FROM memories").fetchone()["n"])

    def test_exact_duplicate(self):
        m = self.stack.memory
        a = m.remember(BOT, UID, "主人养了一只叫团子的猫，橘色的。", importance=0.5)
        b = m.remember(BOT, UID, "主人养了一只叫团子的猫，橘色的。", importance=0.8)
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(self._count(), 1)
        row = self.stack.conn.execute(
            "SELECT importance FROM memories WHERE id=?", (a["memory_id"],)).fetchone()
        # 新规则（memory_engine.py:657）：重复出现 = 更重要，取 max(旧,新) + 0.03 封顶 1.0。
        # 老 store 是直接覆盖成 0.8，所以这里从 0.8 改成 0.83。
        self.assertAlmostEqual(float(row["importance"]), 0.83, places=3)

    def test_near_duplicate_punctuation(self):
        m = self.stack.memory
        a = m.remember(BOT, UID, "2026-11-06 晚上，主人吃了牛杂和小烧烤，还拍了照片发给我看。")
        b = m.remember(BOT, UID, "2026-11-06 晚上主人吃了牛杂和小烧烤还拍了照片发给我看！")
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["memory_id"], b["memory_id"])
        self.assertEqual(self._count(), 1)
        row = self.stack.conn.execute(
            "SELECT merged_count FROM memories WHERE id=?", (a["memory_id"],)).fetchone()
        self.assertGreaterEqual(int(row["merged_count"] or 1), 2)

    def test_dedupe_can_be_disabled(self):
        off = Stack(self.tmp, "nod.db", settings={"auto_dedupe": False})
        try:
            off.memory.remember(BOT, UID, "主人喜欢喝美式咖啡。")
            off.memory.remember(BOT, UID, "主人喜欢喝美式咖啡。")
            n = int(off.conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])
            self.assertEqual(n, 2)
        finally:
            off.close()


class ForgettingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-forget-")
        self.stack = Stack(self.tmp, "amo_data.db")

    def tearDown(self):
        self.stack.close()

    def test_old_memory_sinks(self):
        old = int(time.time()) - 60 * 86400
        m = self.stack.memory
        m.remember(BOT, UID, "六十天前的旧事一件", importance=0.5,
                   created_at=old, last_hit_at=old)
        m.remember(BOT, UID, "刚刚说的新鲜事一件", importance=0.5)
        rows = self.stack.retrieval.recall(BOT, UID, "")
        self.assertIn("新鲜", rows[0]["content"])

    def test_high_importance_decays_slower(self):
        old = int(time.time()) - 60 * 86400
        m = self.stack.memory
        m.remember(BOT, UID, "很重要的事：生日是十二月十号", importance=0.95,
                   created_at=old, last_hit_at=old)
        m.remember(BOT, UID, "不重要的小事一桩", importance=0.2)
        rows = self.stack.retrieval.recall(BOT, UID, "")
        self.assertIn("生日", rows[0]["content"])

    def test_migrated_created_at_kept(self):
        old = int(time.time()) - 100 * 86400
        r = self.stack.memory.remember(BOT, UID, "迁移过来的老记忆", created_at=old)
        self.assertLessEqual(r["created_at"], old + 5)


if __name__ == "__main__":
    unittest.main()
