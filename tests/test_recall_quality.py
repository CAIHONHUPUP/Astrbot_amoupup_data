# -*- coding: utf-8 -*-
"""召回质量回归（memory-recall-eval，2026-11-17）。

固定样本 + 一次性临时库，不读真实库；每一条都对应评估里发现的具体问题：

1. 证据词构造：先删虚词再配对会把「迟到」「记住」拆碎（真实库里 2 条查询
   因此整条空召回）。现在按原始字序配对，只丢虚词对/连接词对；
2. 「迟到罚款」「记住大米」必须召回（修复前为空）；
3. 无关问题不许硬推（「我想学钢琴」必须空召回，防止证据词放宽后回潮）；
4. 注入块里不许出现近重复（默认 diversify_ratio=0.6），但也不许把不同事实合并。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from support.store_fixture import DataStore  # noqa: E402
from utils.text_process import (  # noqa: E402
    DEFAULT_SETTINGS, GLUE_CHARS, STOP_CHARS, _informative_terms, _jaccard,
    _query_chars, _recall_tokens,
)

BOT = "bot"
UID = "u1"

#: 固定样本：一条带虚词的罚款流水、一条「记住」类事实、两条近重复的火锅偏好、
#: 两条不相干的事实（用来验证「不过度合并」）
ROWS = (
    ("2026-11-17 00:00 支出 95.93 元（迟到罚款），余额 6,235.02 元", "finance_long_expense", 0.75),
    ("family曾让Bot记住“大米”一事，Bot在飞书上被问及时能回忆起来。", "stable_fact", 0.70),
    # 这一对二元组 Jaccard=0.735：旧默认 0.75 会两条都注入，新默认 0.60 只留一条
    ("今天和老公去江边散步，风很大", "milestone", 0.75),
    ("今天和老公去江边散步，风很大，还放了风筝", "milestone", 0.75),
    ("主人养了一只叫团子的橘猫", "fact", 0.60),
    ("主人对芒果过敏，不能吃", "fact", 0.90),
)


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class EvidenceTermTests(unittest.TestCase):
    """证据词构造：这是「迟到罚款」空召回的直接原因。"""

    def test_adjacent_words_with_stop_chars_survive(self):
        terms = set(_informative_terms("迟到罚了多少钱"))
        self.assertIn("迟到", terms, "「迟到」的「到」是虚词，但它是词的一部分，不能被拆掉")
        terms = set(_informative_terms("我让你记住的那两个字是什么"))
        self.assertIn("记住", terms, "「记住」的「记」是虚词，但它是词的一部分")
        terms = set(_informative_terms("我这个月花了多少钱"))
        self.assertIn("个月", terms)

    def test_glue_pairs_are_not_evidence(self):
        # 这些二元组只是「代词/连接词 + 字」，不是词；放进来会让什么都能命中
        for query, junk in (("我想学钢琴", "我想"), ("周杰伦的新歌好听吗", "的新"),
                            ("昨晚谁先睡的", "晚谁")):
            terms = set(_informative_terms(query))
            self.assertNotIn(junk, terms)
            self.assertTrue(set(junk) & GLUE_CHARS, "这类垃圾二元组由 GLUE_CHARS 挡掉")
        # 纯虚词对（了吗/多少）照样要丢
        self.assertNotIn("了吗", set(_informative_terms("睡了吗")))
        self.assertNotIn("多少", set(_informative_terms("多少钱")))

    def test_query_chars_still_drops_stop_chars(self):
        self.assertEqual(_query_chars("我吃了什么"), ["吃"])
        self.assertFalse(set(_query_chars("迟到罚了多少钱")) & STOP_CHARS)


class RecallEvidenceFixTests(unittest.TestCase):
    """真实库里修掉的两条空召回，用固定样本钉住。"""

    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo.db"))
        for content, kind, importance in ROWS:
            self.store.remember(BOT, UID, content, kind=kind, importance=importance)

    def tearDown(self):
        self.store.close()

    def test_fine_for_late_arrival_is_recalled(self):
        rows = self.store.recall(BOT, UID, "迟到罚了多少钱", limit=6)
        self.assertTrue(rows, "「迟到罚款」修复前整条空召回")
        self.assertIn("迟到罚款", rows[0]["content"])

    def test_remember_word_is_recalled(self):
        rows = self.store.recall(BOT, UID, "我让你记住的那两个字是什么", limit=6)
        self.assertTrue(rows, "「记住」修复前整条空召回")
        self.assertTrue(any("大米" in row["content"] for row in rows))

    def test_unrelated_query_still_abstains(self):
        # 证据词放宽后最容易回潮的假阳性：库里根本没有钢琴
        self.assertEqual(self.store.recall(BOT, UID, "我想学钢琴", limit=6), [])
        self.assertEqual(self.store.recall(BOT, UID, "周杰伦的新歌好听吗", limit=6), [])


class InjectionDedupeTests(unittest.TestCase):
    """注入去重：不许近重复一起注入，也不许把不同事实合并。"""

    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo.db"))
        for content, kind, importance in ROWS:
            self.store.remember(BOT, UID, content, kind=kind, importance=importance)

    def tearDown(self):
        self.store.close()

    def test_default_ratio_is_stricter_than_write_time_dedupe(self):
        # 注入去重（0.60）必须比写时近重复合并（0.92）严：前者只影响这次注入，
        # 后者会改库，所以两者的阈值不能共用一个数（见 REPORT）。
        self.assertLessEqual(DEFAULT_SETTINGS["diversify_ratio"], 0.75)
        self.assertLess(DEFAULT_SETTINGS["diversify_ratio"],
                        DEFAULT_SETTINGS["near_duplicate_ratio"])

    def test_injected_block_has_no_near_duplicate_pair(self):
        rows = self.store.recall(BOT, UID, "江边散步", limit=6)
        self.assertTrue(rows)
        toks = [set(_recall_tokens(row["content"])) for row in rows]
        for i in range(len(toks)):
            for j in range(i + 1, len(toks)):
                self.assertLess(
                    _jaccard(toks[i], toks[j]), DEFAULT_SETTINGS["diversify_ratio"],
                    "注入块里出现了近重复：%r / %r" % (rows[i]["content"], rows[j]["content"]))

    def test_old_threshold_would_have_kept_both(self):
        """同一对记忆：0.75 时两条都注入，0.60 时只留一条（这正是本次改动）。"""
        first = "今天和老公去江边散步，风很大"
        second = "今天和老公去江边散步，风很大，还放了风筝"
        ratio = _jaccard(_recall_tokens(first), _recall_tokens(second))
        self.assertGreaterEqual(ratio, 0.60, "这一对必须落在新阈值里，测试才有意义")
        self.assertLess(ratio, 0.75, "这一对必须落在旧阈值外，否则测不出改动")
        loose = DataStore(os.path.join(self.tmp, "loose.db"),
                          settings={"diversify_ratio": 0.75})
        try:
            loose.remember(BOT, UID, first, kind="milestone", importance=0.75)
            loose.remember(BOT, UID, second, kind="milestone", importance=0.75)
            rows = loose.recall(BOT, UID, "江边散步", limit=6)
            self.assertEqual(len([r for r in rows if "江边散步" in r["content"]]), 2)
        finally:
            loose.close()
        tight = DataStore(os.path.join(self.tmp, "tight.db"))
        try:
            tight.remember(BOT, UID, first, kind="milestone", importance=0.75)
            tight.remember(BOT, UID, second, kind="milestone", importance=0.75)
            rows = tight.recall(BOT, UID, "江边散步", limit=6)
            self.assertEqual(len([r for r in rows if "江边散步" in r["content"]]), 1)
        finally:
            tight.close()

    def test_distinct_facts_are_not_over_merged(self):
        cat = self.store.recall(BOT, UID, "团子", limit=6)
        mango = self.store.recall(BOT, UID, "芒果", limit=6)
        self.assertTrue(any("团子" in row["content"] for row in cat))
        self.assertTrue(any("芒果" in row["content"] for row in mango))
        both = self.store.recall(BOT, UID, "过敏", limit=6)
        self.assertTrue(both, "「过敏」不能因为去重被整条吞掉")


class EvaluationHarnessContractTests(unittest.TestCase):
    """评估脚本必须可复跑、且不会把真实库当输入。"""

    def test_harness_refuses_real_db(self):
        sys.path.insert(0, os.path.join(ROOT, "work"))
        import recall_common as rc
        with self.assertRaises(RuntimeError):
            rc.copy_db(rc.REAL_DB, _tmpdir())

    def test_corpus_covers_required_categories(self):
        import json
        path = os.path.join(ROOT, "work", "corpus_real.json")
        with open(path, encoding="utf-8") as fh:
            corpus = json.load(fh)
        cats = {q["cat"] for q in corpus["queries"]}
        for required in ("人物", "事件", "偏好", "时间", "元问题", "无答案"):
            self.assertIn(required, cats)
        self.assertGreaterEqual(len(corpus["queries"]), 50)


if __name__ == "__main__":
    unittest.main()
