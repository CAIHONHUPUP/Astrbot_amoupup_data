# -*- coding: utf-8 -*-
"""记忆写入带 event:<id>，能按标签查回（M1 契约，2026-11-17）。

守护/陪伴（amo）写活动记忆时 tags 里带 "#活动 event:<id>"：这条长期记忆和日程块 /
账本 / 块日记说的是同一个事件。本文件钉住四件事：

1. 写进去的 event:<id> 真落库，并且能按这个标签查回来（整词匹配，不串号）；
2. 没有 event 的老记忆照样能按关键词召回，标签过滤不会把它们误捞出来；
3. 老库 tags 为 NULL / 压根没传 tags 时不崩，读出来当空标签；
4. 同 idem 幂等 / 近重复合并不把标签弄丢（去重语义不被标签破坏）。

写入形状与 amo `_memory_coro` -> `_data_memory_write` 逐字段一致：
kind=activity, importance=0.25, scope=private, subject=bot, idem_key=activity:<day>:<start>。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from core.api import MemoryAPI  # noqa: E402
from support.store_fixture import DataStore  # noqa: E402

BOT = "大肥鱼"
UID = "302578963"
EVENT = "evt-20261116-1400"
TAG = "#活动 event:%s" % EVENT
IDEM = "activity:2026-11-16:14:00"
CONTENT = "下午和柳如烟在商场逛街"


class EventTagCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="amo-data-event-")
        self.store = DataStore(
            os.path.join(self.tmp, "amo_data.db"),
            {"embedding_enabled": False, "embedding_backfill": False},
        )
        self.api = MemoryAPI(self.store)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:  # 关库失败不该把断言结果盖掉
            pass

    def write_activity(self, text=CONTENT, event=EVENT, idem=IDEM, tags=None) -> dict:
        """按 amo 活动记忆的写入形状落一条（tags 默认带 event:<id>）。"""
        if tags is None:
            tags = ("#活动 event:%s" % event) if event else "#活动"
        return self.store.memory.remember(
            BOT, UID, text, kind="activity", importance=0.25, scope="private",
            group_id="", subject="bot", platform="", idem_key=idem, tags=tags,
        )

    def recall(self, query: str = "", tags: str = "") -> list:
        return self.store.retrieval.recall(BOT, UID, query, 6, "private", "", tags=tags)

    def rows(self) -> list:
        return self.store.memory.list_memories(BOT, UID, 20)


class EventTagRoundtripTests(EventTagCase):
    def test_amo_bridge_write_shape_is_queryable_by_event_tag(self) -> None:
        """写进去的 event:<id> 能当查询条件，空关键词也能查回这条记忆。"""
        wrote = self.write_activity()
        self.assertTrue(wrote.get("ok", True), wrote)

        items = self.recall("", tags="event:%s" % EVENT)
        self.assertEqual(len(items), 1, items)
        item = items[0]
        self.assertEqual(item["content"], CONTENT)
        self.assertEqual(item["tags"], TAG, "tags 里必须留着 event:<id>")
        self.assertEqual(item["subject"], "bot")
        self.assertAlmostEqual(float(item["importance"]), 0.25, places=4)

    def test_keyword_recall_still_finds_the_same_memory(self) -> None:
        """标签是附加维度：原来的关键词召回还能查到同一条。"""
        self.write_activity()
        items = self.recall("逛街")
        self.assertEqual([item["content"] for item in items], [CONTENT])

    def test_tag_filter_is_token_exact(self) -> None:
        """event:...1400 不能把 event:...14001 也捞出来（整词匹配，不串号）。"""
        self.write_activity()
        self.write_activity(text="上午在医院排队", event=EVENT + "1", idem=IDEM + "1")
        only_first = self.recall("", tags="event:%s" % EVENT)
        self.assertEqual([item["content"] for item in only_first], [CONTENT])
        only_second = self.recall("", tags="event:%s1" % EVENT)
        self.assertEqual([item["content"] for item in only_second], ["上午在医院排队"])


class LegacyCompatibilityTests(EventTagCase):
    def test_old_memory_without_event_still_recallable_by_keyword(self) -> None:
        """没有 event 的老记忆：关键词召回不变，标签过滤不会误捞它。"""
        self.store.memory.remember(BOT, UID, "他喜欢喝美式咖啡", kind="fact",
                                   importance=0.5)
        hits = self.recall("咖啡")
        self.assertEqual([item["content"] for item in hits], ["他喜欢喝美式咖啡"])
        self.assertEqual(hits[0]["tags"], "", "老记忆的标签应为空串")
        self.assertEqual(self.recall("", tags="event:%s" % EVENT), [])

    def test_tag_filter_with_keyword_does_not_leak_untagged_rows(self) -> None:
        """带标签的过滤 + 关键词：只回带标签那条，同话题的普通记忆不混进来。"""
        self.write_activity()
        self.store.memory.remember(BOT, UID, "昨天和柳如烟逛街买了鞋", kind="fact",
                                   importance=0.5)
        tagged = self.recall("逛街", tags="event:%s" % EVENT)
        self.assertEqual([item["content"] for item in tagged], [CONTENT])
        everything = self.recall("逛街")
        self.assertEqual(len(everything), 2, everything)

    def test_null_tags_row_does_not_crash(self) -> None:
        """老库迁移前可能存在 tags=NULL：列表/召回都要当成空标签，不能抛。"""
        wrote = self.write_activity()
        self.store.conn.execute("UPDATE memories SET tags=NULL WHERE id=?",
                                (int(wrote["memory_id"]),))
        self.store.conn.commit()

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], "")
        self.assertEqual([item["content"] for item in self.recall("逛街")], [CONTENT])
        self.assertEqual(self.recall("", tags="event:%s" % EVENT), [])

    def test_legacy_callers_without_tags_still_work(self) -> None:
        """tags 是后加参数：完全不带 tags 的旧调用不能崩，也不能凭空长出标签。"""
        self.store.memory.remember(BOT, UID, "她养了一只叫团子的橘猫", kind="fact",
                                   importance=0.5)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], "")
        self.assertNotEqual(asyncio.run(self.api.remember(
            BOT, UID, "她明天要早起", kind="fact")), {"ok": False})


class IdempotencyTests(EventTagCase):
    def test_same_idem_keeps_one_row_and_the_event_tag(self) -> None:
        """同一个活动重复收尾（同 idem）：只留一行，event 标签不被覆盖成空。"""
        self.write_activity()
        self.write_activity()
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["tags"], TAG)
        self.assertEqual(len(self.recall("", tags="event:%s" % EVENT)), 1)

        # 再写一次却没带 tags（旧调用方）：旧标签必须留着
        self.write_activity(tags="")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], TAG, "空 tags 不许清掉旧 event 标签")

    def test_near_duplicate_merge_keeps_event_tag(self) -> None:
        """近重复合并路径：同一件事第二次带标签写，标签要并到那一行上。"""
        self.store.memory.remember(BOT, UID, "我喜欢喝美式咖啡", kind="fact",
                                   importance=0.5)
        self.store.memory.remember(BOT, UID, "我喜欢喝美式咖啡。", kind="fact",
                                   importance=0.5, tags=TAG)
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["tags"], TAG)
        self.assertEqual(len(self.recall("", tags="event:%s" % EVENT)), 1)


class ApiForwardingTests(EventTagCase):
    def test_api_recall_forwards_tags(self) -> None:
        """amo 侧的 `mem.recall(...)` 是门面：tags 必须透传到检索层。"""
        self.write_activity()
        items = asyncio.run(self.api.recall(BOT, UID, "", 6, "private", "",
                                            tags="event:%s" % EVENT))
        self.assertEqual([item["content"] for item in items], [CONTENT])
        self.assertEqual(items[0]["tags"], TAG)
        other = asyncio.run(self.api.recall(BOT, UID, "", 6, "private", "",
                                            tags="event:other"))
        self.assertEqual(other, [])

    def test_api_recall_legacy_positional_call_still_works(self) -> None:
        """老调用按位置传参（最多到 pid）：不传 tags 也得照常召回。"""
        self.write_activity()
        items = asyncio.run(self.api.recall(BOT, UID, "逛街", 5))
        self.assertEqual([item["content"] for item in items], [CONTENT])


if __name__ == "__main__":
    unittest.main()
