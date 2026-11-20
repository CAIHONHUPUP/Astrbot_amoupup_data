# -*- coding: utf-8 -*-
"""产品级行为规格测试：给定 X → 必须 Y（想记 / Astrbot_amoupup_data，弱点 4）。

只通过 DataStore 的对外入口断言「用户可观察行为」，不复制实现细节；
每条用例在 work/reverse_spec_data.py 里都有一次「把生产规则改坏」的反向验证，
改坏后该用例必须变红（脚本会打印 RED 表）。

规格清单
--------
A. 同会话隔离（SpecScopeIsolationTests）
  A1 test_spec_idem_dedupe_isolated_by_scope_and_group
     给定：同一 vault、同一 uid，在私聊 / 群 g1 / 群 g2 用同一 idem_key 写同一句话。
     必须：前三条都算新写入；只在同一 (scope, group_id) 里再写一次才算 duplicate；
           总条数 3，私聊 / g1 / g2 各 1 条。
  A2 test_spec_recall_isolated_by_scope_and_group
     给定：私聊讲出差、群 g1 讲爬山、群 g2 讲游戏。
     必须：私聊召回「爬山」为空；g2 召回「爬山」为空；只有 g1 召回爬山；
           私聊召回「出差」只出私聊那条。群聊内容绝不出现在私聊召回里。
  A3 test_spec_summary_merge_isolated_by_scope_and_group
     给定：同一天两条私聊摘要 + 一条群摘要。
     必须：compress_memories 只合并私聊那两条；群摘要独立保留；
           私聊合并正文不含群聊内容，群摘要不含私聊内容。

B. 元问题兜底（SpecMetaFallbackTests）
  B1 test_spec_meta_question_zero_hits_falls_back_to_2_or_3
     给定：六条记忆，问「你还记得我吗 / 你记得什么 / 你知道我是谁吗」。
     必须：关键词命中 0 条时兜底返回 2~3 条，全部标记 meta_fallback、
           matched_terms 为 0，按 score 降序，最高重要度那条排第一。
  B2 test_spec_meta_fallback_ranks_by_importance_then_freshness
     给定：高重要度 1 天前、中重要度 1 天前、中重要度 300 天前、低重要度现在。
     必须：返回 3 条，顺序为 高 → 中(新) → 中(旧)；同等重要度时新的在前。
  B3 test_spec_meta_fallback_respects_scope
     给定：私聊一条、群 g1 一条，都问元问题。
     必须：私聊兜底只返回私聊记忆，群兜底只返回群记忆。
  B4 test_spec_factful_question_still_requires_relevance
     给定：有「生日」事实 + 两条高重要度干扰事实，问「你记得我生日吗」。
     必须：只返回生日那条且不带 meta_fallback；问「你还记得我住哪吗」返回空。
           即带实词的提问严格走相关性、绝不兜底。

C. 长摘要拆分（SpecLongSummaryTests，summary_merge_max_chars=800）
  C1 test_spec_long_summary_split_into_bounded_pieces
     给定：一条 ~1800 字、远超 800 的对话摘要。
     必须：split_long_summaries 至少拆成 3 条，每条非空且 <= 800。
  C2 test_spec_long_summary_split_is_lossless
     给定：同一条超长摘要。
     必须：按 id 顺序拼回各条，逐字等于原文（标点、空格都不丢）。
  C3 test_spec_week_merge_stops_at_cap
     给定：同一天两条 ~430 字摘要，拼起来超过 800。
     必须：compress_memories 不合并（merged_summaries=0），两条都保持 active，
           没有任何一条超过 800。
  C4 test_spec_week_merge_within_cap_still_merges
     给定：同一天两条 ~170 字摘要，拼起来不超过 800。
     必须：compress_memories 合成一条（merged_summaries=1），正文 <= 800
           且两条信息都还在。

固定时间：每个用例 setUp 里 utils.clock.freeze(2026-11-18 15:00)，
tearDown 的 finally 里 unfreeze，避免把冻结状态漏给别的用例。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
for _path in (ROOT, TESTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:  # 单文件直接跑（不经过 conftest）时也要能 import astrbot
    import astrbot_stub

    astrbot_stub.install()
except Exception:  # pragma: no cover - 已安装/无桩时保持原状
    pass

from support.store_fixture import DataStore  # noqa: E402
from utils.clock import freeze, unfreeze  # noqa: E402

FROZEN_AT = datetime(2026, 11, 18, 15, 0)
CAP = 800

#: 三段词面互不相同的摘要素材，避免被「近重复合并」误伤
UNIT_A = "上午在公司开会讨论季度目标, 大家意见不太统一。"
UNIT_B = "晚上回家做了饭, 还看了一集纪录片, 心情不错。"
UNIT_LONG = "他讲了上班路上的见闻, 还提到晚饭吃了鱼。"


def _long_summary(day: str, unit: str, repeats: int) -> str:
    """造一条带日期前缀、按句号分句的超长摘要。"""
    return day + " " + "".join("%02d%s" % (index, unit) for index in range(repeats))


class SpecCase(unittest.TestCase):
    """规格用例公共夹具：临时库 + 固定时间 + 对外行为封装。"""

    settings = None

    def setUp(self) -> None:
        self._frozen_at = freeze(FROZEN_AT)
        self.now_ts = int(self._frozen_at.timestamp())
        self._tmp = tempfile.mkdtemp(prefix="amo-spec-")
        self.bot = "bot-spec"
        self.uid = "10001"
        self.store = DataStore(os.path.join(self._tmp, "amo_data.db"), settings=self.settings)

    def tearDown(self) -> None:
        try:
            if getattr(self, "store", None) is not None:
                self.store.close()
        finally:
            shutil.rmtree(getattr(self, "_tmp", ""), ignore_errors=True)
            unfreeze()

    # ---- 对外行为的最小封装，断言里不出现 SQL ----
    def write(self, content: str, **kwargs):
        result = self.store.remember(self.bot, self.uid, content, **kwargs)
        self.assertTrue(result.get("ok"), "写入失败：%r" % (result,))
        return result

    def recall(self, query: str = "", scope: str = "", group_id: str = ""):
        return self.store.recall(self.bot, self.uid, query, limit=50,
                                 scope=scope, group_id=group_id)

    def active_summaries(self, scope: str = "", group_id: str = ""):
        return [row for row in self.recall("", scope=scope, group_id=group_id)
                if str(row.get("kind")) == "conversation_summary"]


class SpecScopeIsolationTests(SpecCase):
    """规格组 A：同一 vault 里私聊 / 群 g1 / 群 g2 的写入、召回、摘要分组都必须隔离。"""

    def test_spec_idem_dedupe_isolated_by_scope_and_group(self) -> None:
        first = self.write("他说明天要出差", kind="event",
                           scope="private", idem_key="trip")
        second = self.write("他说明天要出差", kind="event",
                            scope="group", group_id="g1", idem_key="trip")
        third = self.write("他说明天要出差", kind="event",
                           scope="group", group_id="g2", idem_key="trip")
        again = self.write("他说明天要出差", kind="event",
                           scope="private", idem_key="trip")

        self.assertFalse(first.get("duplicate"), "私聊第一次写入不许算重复")
        self.assertFalse(second.get("duplicate"), "群 g1 与私聊的 idem_key 不能互相撞")
        self.assertFalse(third.get("duplicate"), "群 g2 与群 g1 的 idem_key 不能互相撞")
        self.assertTrue(again.get("duplicate"), "同一会话再说一遍仍然要合并")
        self.assertEqual(first["memory_id"], again["memory_id"])
        self.assertEqual(self.store.stats(self.bot)["memories"], 3)
        self.assertEqual(len(self.recall("", scope="private")), 1)
        self.assertEqual(len(self.recall("", scope="group", group_id="g1")), 1)
        self.assertEqual(len(self.recall("", scope="group", group_id="g2")), 1)

    def test_spec_recall_isolated_by_scope_and_group(self) -> None:
        self.write("我下周要去杭州出差, 顺便见客户", kind="fact",
                   importance=0.8, scope="private")
        self.write("群里约了周末去爬山, 他说想去又怕累", kind="fact",
                   importance=0.8, scope="group", group_id="g1")
        self.write("另一个群在讨论新出的游戏", kind="fact",
                   importance=0.8, scope="group", group_id="g2")

        self.assertEqual(self.recall("爬山", scope="private"), [],
                         "群聊内容绝不允许出现在私聊召回里")
        self.assertEqual(self.recall("爬山", scope="group", group_id="g2"), [],
                         "g2 的召回不许串到 g1 的记忆")
        g1_hits = self.recall("爬山", scope="group", group_id="g1")
        self.assertEqual(len(g1_hits), 1)
        self.assertIn("爬山", g1_hits[0]["content"])
        private_hits = self.recall("出差", scope="private")
        self.assertEqual(len(private_hits), 1)
        self.assertIn("出差", private_hits[0]["content"])
        self.assertNotIn("爬山", private_hits[0]["content"])

    def test_spec_summary_merge_isolated_by_scope_and_group(self) -> None:
        self.write("2026-11-08 私聊里他讲了换工作的事, 说还没定下来",
                   kind="conversation_summary", importance=0.6, scope="private")
        self.write("2026-11-08 私聊又聊到搬家的计划, 想搬到离公司近一点",
                   kind="conversation_summary", importance=0.6, scope="private")
        self.write("2026-11-08 群里在约周末爬山, 他说想去又怕累",
                   kind="conversation_summary", importance=0.6,
                   scope="group", group_id="g1")

        result = self.store.compress_memories(self.bot, apply=True)
        self.assertEqual(result["merged_summaries"], 1,
                         "同一天的两条私聊摘要必须合并，群摘要不能一起进来")

        private_rows = self.active_summaries(scope="private")
        group_rows = self.active_summaries(scope="group", group_id="g1")
        self.assertEqual(len(private_rows), 1)
        self.assertEqual(len(group_rows), 1)
        private_text = private_rows[0]["content"]
        group_text = group_rows[0]["content"]
        self.assertIn("换工作", private_text)
        self.assertIn("搬家", private_text)
        self.assertNotIn("爬山", private_text, "群聊内容一个字都不许进私聊摘要")
        self.assertIn("爬山", group_text)
        self.assertNotIn("换工作", group_text)
        self.assertNotIn("搬家", group_text)


class SpecMetaFallbackTests(SpecCase):
    """规格组 B：无实词的元问题兜底 2~3 条；带实词的提问严格走相关性。"""

    HIGH = "我养了一只叫团子的猫"

    def test_spec_meta_question_zero_hits_falls_back_to_2_or_3(self) -> None:
        self.write(self.HIGH, importance=0.95, created_at=self.now_ts - 86400)
        self.write("我最喜欢的颜色是蓝色", importance=0.80,
                   created_at=self.now_ts - 2 * 86400)
        self.write("我下个月要搬家", importance=0.60,
                   created_at=self.now_ts - 3 * 86400)
        self.write("我最近在学吉他", importance=0.40,
                   created_at=self.now_ts - 4 * 86400)
        self.write("我上周去看了海", importance=0.20,
                   created_at=self.now_ts - 5 * 86400)
        self.write("昨天吃了火锅", importance=0.10,
                   created_at=self.now_ts - 6 * 86400)

        for question in ("你还记得我吗", "你记得什么", "你知道我是谁吗"):
            items = self.recall(question, scope="private")
            self.assertGreaterEqual(len(items), 2, "元问题兜底至少要给 2 条：%s" % question)
            self.assertLessEqual(len(items), 3, "元问题兜底最多 3 条：%s" % question)
            for item in items:
                self.assertTrue(item.get("meta_fallback"),
                                "元问题兜底条目必须带 meta_fallback 标记")
                self.assertEqual(item.get("matched_terms"), 0,
                                 "这条问题本来就没有实词命中")
            self.assertEqual(items[0]["content"], self.HIGH,
                             "最高重要度 + 最新鲜的那条应排第一")
            scores = [item["score"] for item in items]
            self.assertEqual(scores, sorted(scores, reverse=True),
                             "兜底结果必须按综合分降序")

    def test_spec_meta_fallback_ranks_by_importance_then_freshness(self) -> None:
        self.write("我养了一只叫团子的猫", importance=0.9,
                   created_at=self.now_ts - 86400)
        self.write("我最喜欢的颜色是蓝色", importance=0.5,
                   created_at=self.now_ts - 86400)
        self.write("我下个月要搬家", importance=0.5,
                   created_at=self.now_ts - 300 * 86400)
        self.write("昨天吃了火锅", importance=0.1, created_at=self.now_ts)

        items = self.recall("你还记得我吗", scope="private")
        self.assertEqual([item["content"] for item in items], [
            "我养了一只叫团子的猫",
            "我最喜欢的颜色是蓝色",
            "我下个月要搬家",
        ], "先按重要度，再按新鲜度：中重要度里 1 天前的必须排在 300 天前的前面")
        self.assertEqual(len(items), 3)
        self.assertGreater(items[1]["created_at"], items[2]["created_at"])
        scores = [item["score"] for item in items]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_spec_meta_fallback_respects_scope(self) -> None:
        self.write("私聊里他说最近总失眠", importance=0.9, scope="private")
        self.write("群里在讨论周末的比赛", importance=0.95,
                   scope="group", group_id="g1")

        private_items = self.recall("你还记得我吗", scope="private")
        group_items = self.recall("你还记得我吗", scope="group", group_id="g1")
        self.assertEqual(len(private_items), 1)
        self.assertIn("失眠", private_items[0]["content"])
        self.assertNotIn("比赛", private_items[0]["content"],
                         "私聊兜底不许带出群聊记忆")
        self.assertEqual(len(group_items), 1)
        self.assertIn("比赛", group_items[0]["content"])
        self.assertNotIn("失眠", group_items[0]["content"])

    def test_spec_factful_question_still_requires_relevance(self) -> None:
        birthday = self.write("我的生日是十一月二十号", kind="fact", importance=0.9)
        self.write("我养了一只叫团子的猫", kind="fact", importance=0.95)
        self.write("我下个月要搬家", kind="fact", importance=0.8)

        items = self.recall("你记得我生日吗", scope="private")
        self.assertEqual(len(items), 1, "带实词的提问只许返回真正相关的那条")
        self.assertEqual(items[0]["id"], birthday["memory_id"])
        self.assertIn("生日", items[0]["content"])
        self.assertFalse(items[0].get("meta_fallback"),
                         "带实词的提问绝不走元问题兜底")
        self.assertEqual(self.recall("你还记得我住哪吗", scope="private"), [],
                         "没有相关记忆时带实词的提问必须返回空，而不是兜底")


class SpecLongSummaryTests(SpecCase):
    """规格组 C：超长摘要按句拆条、不丢内容，周整理合并也受同一上限约束。"""

    settings = {"summary_merge_max_chars": CAP}

    def test_spec_long_summary_split_into_bounded_pieces(self) -> None:
        text = _long_summary("2026-11-01", UNIT_LONG, 80)
        self.assertGreater(len(text), CAP * 2, "素材本身要远超上限")
        self.write(text, kind="conversation_summary", importance=0.7)

        result = self.store.split_long_summaries(self.bot, CAP)
        self.assertEqual(result["summaries"], 1)
        self.assertGreaterEqual(result["created"], 2)
        pieces = self.active_summaries()
        self.assertGreaterEqual(len(pieces), 3, "超长摘要必须拆成多条")
        for piece in pieces:
            self.assertTrue(piece["content"], "拆出来的条不能是空的")
            self.assertLessEqual(len(piece["content"]), CAP,
                                 "每条都不得超过 summary_merge_max_chars")

    def test_spec_long_summary_split_is_lossless(self) -> None:
        text = _long_summary("2026-11-01", UNIT_LONG, 80)
        self.write(text, kind="conversation_summary", importance=0.7)
        self.store.split_long_summaries(self.bot, CAP)

        pieces = sorted(self.active_summaries(), key=lambda row: int(row["id"]))
        self.assertGreaterEqual(len(pieces), 2)
        self.assertEqual("".join(row["content"] for row in pieces), text,
                         "按 id 顺序拼回去必须逐字等于原文")

    def test_spec_week_merge_stops_at_cap(self) -> None:
        first = _long_summary("2026-11-02", UNIT_A, 16)
        second = _long_summary("2026-11-02", UNIT_B, 16)
        self.assertGreater(len(first) + len(second), CAP,
                           "素材拼起来必须超过上限")
        self.assertLessEqual(max(len(first), len(second)), CAP,
                             "单条本身不超上限，排除拆条逻辑干扰")
        self.write(first, kind="conversation_summary", importance=0.6)
        self.write(second, kind="conversation_summary", importance=0.6)

        result = self.store.compress_memories(self.bot, apply=True)
        self.assertEqual(result["split_summaries"], 0)
        self.assertEqual(result["merged_summaries"], 0,
                         "合并后正文会超上限，必须停手，绝不带病标 merged")
        self.assertEqual(result["merged_near_duplicates"], 0)
        rows = self.active_summaries()
        self.assertEqual({row["content"] for row in rows}, {first, second})
        for row in rows:
            self.assertLessEqual(len(row["content"]), CAP)

    def test_spec_week_merge_within_cap_still_merges(self) -> None:
        first = _long_summary("2026-11-03", UNIT_A, 6)
        second = _long_summary("2026-11-03", UNIT_B, 6)
        self.assertLessEqual(len(first) + len(second), CAP,
                             "素材拼起来必须在上限内")
        self.write(first, kind="conversation_summary", importance=0.6)
        self.write(second, kind="conversation_summary", importance=0.6)

        result = self.store.compress_memories(self.bot, apply=True)
        self.assertEqual(result["merged_summaries"], 1,
                         "上限内的同天摘要仍必须合并")
        rows = self.active_summaries()
        self.assertEqual(len(rows), 1)
        merged = rows[0]["content"]
        self.assertLessEqual(len(merged), CAP)
        self.assertIn("上午", merged)
        self.assertIn("晚上", merged)


if __name__ == "__main__":
    unittest.main()
