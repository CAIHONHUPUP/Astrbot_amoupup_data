# -*- coding: utf-8 -*-
"""第二轮 P1/P2 回归：夜间提炼的「流水/摘要范围」与「ids 所有权」。

证据链（2026-11-16）：
* 守护插件 Astrbot_amoupup_watch/services/nightly_memory.py 把
  data.nightly_memory_candidates(hours=24, limit=300) 的结果按 (bot_id, uid, scope,
  group_id, subject) 分组，组内 **全部** 行的 id 直接传给 nightly_apply 归档；
* 而 dao.recent_active 以前不过滤 kind，daily_digest / conversation_summary 也会被
  当零碎流水送进 LLM，提炼成功后一起 archived —— 日总结从此不参与召回；
* nightly_apply 以前完全信任调用方给的 ids，别的 bot/uid 的记忆也会被归档。
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

from main import MemoryAPI  # noqa: E402
from support.store_fixture import DataStore  # noqa: E402

BOT = "default"
UID = "302578963"
OTHER_UID = "999999999"


class NightlyScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="amo-data-nightly-scope-")
        self.store = DataStore(
            os.path.join(self.tmp, "amo_data.db"),
            {"embedding_enabled": False, "embedding_backfill": False},
        )
        self.api = MemoryAPI(self.store)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass

    def meta(self, bot_id: str = BOT, uid: str = UID) -> dict:
        return {"bot_id": bot_id, "uid": uid, "scope": "private",
                "group_id": "", "subject": "user"}

    def status(self, memory_id: int) -> str:
        row = self.store.conn.execute(
            "SELECT status FROM memories WHERE id=?", (int(memory_id),)
        ).fetchone()
        return str(row["status"]) if row else ""

    def candidate_ids(self) -> list:
        return [row["id"] for row in
                self.store.memory.nightly_candidates(hours=24, limit=50)]

    # ---------- 1) 候选只给流水，摘要不进夜间提炼 ----------
    def test_recent_active_excludes_summaries(self) -> None:
        self.store.remember(BOT, UID, "今天下午去公园散步了", kind="event")
        self.store.remember(BOT, UID, "2026-11-15 聊了吃饭和天气。", kind="daily_digest")
        self.store.remember(BOT, UID, "2026-11-15 聊了工作上的事。", kind="conversation_summary")
        rows = self.store.memory.nightly_candidates(hours=24, limit=50)
        self.assertEqual(len(rows), 1, "daily_digest/conversation_summary 不能当选候选")
        self.assertIn("公园", rows[0]["content"])
        self.assertNotIn(str(rows[0]["kind"]), ("daily_digest", "conversation_summary"))

    # ---------- 2) 完整夜间流程后摘要仍然 active 且可召回 ----------
    def test_nightly_flow_keeps_summaries_active(self) -> None:
        self.store.remember(BOT, UID, "今天下午去公园散步了", kind="event")
        digest = self.store.remember(BOT, UID, "2026-11-15 聊了吃饭和天气。",
                                     kind="daily_digest")
        summary = self.store.remember(BOT, UID, "2026-11-15 聊了工作上的事。",
                                      kind="conversation_summary")
        ids = self.candidate_ids()
        out = asyncio.run(self.api.nightly_apply(
            self.meta(), ["[日常] 下午去公园散步"], ids))
        self.assertEqual(out["written"], 1)
        self.assertEqual(out["archived"], 1, "只该归档那一条流水")

        self.assertEqual(self.status(digest["memory_id"]), "active")
        self.assertEqual(self.status(summary["memory_id"]), "active")

        recalled = {row["id"] for row in self.store.recall(BOT, UID, "")}
        self.assertIn(digest["memory_id"], recalled, "日总结被归档后就不再召回")
        self.assertIn(summary["memory_id"], recalled, "会话摘要被归档后就不再召回")

    # ---------- 3) ids 所有权：只归档本人 ----------
    def test_nightly_apply_only_archives_owner(self) -> None:
        mine = self.store.remember(BOT, UID, "我今天去爬山了", kind="event")
        other = self.store.remember(BOT, OTHER_UID, "他今天去看电影了", kind="event")
        ids = self.candidate_ids()
        self.assertEqual(len(ids), 2)

        out = asyncio.run(self.api.nightly_apply(
            self.meta(), ["[日常] 今天去爬山"], ids))
        self.assertEqual(out["written"], 1)
        self.assertEqual(out["archived"], 1, "别人名下的 id 必须被跳过")

        self.assertEqual(self.status(mine["memory_id"]), "archived")
        self.assertEqual(self.status(other["memory_id"]), "active")
        row = self.store.conn.execute(
            "SELECT content FROM memories WHERE id=?", (other["memory_id"],)
        ).fetchone()
        self.assertEqual(row["content"], "他今天去看电影了", "别人的记忆正文不能被改")

    # ---------- 4) ids 所有权：跨 bot 也不许归档 ----------
    def test_nightly_apply_other_bot_not_archived(self) -> None:
        here = self.store.remember("botA", UID, "今天在家做饭", kind="event")
        there = self.store.remember("botB", UID, "今天在公司加班", kind="event")
        ids = self.candidate_ids()
        out = asyncio.run(self.api.nightly_apply(
            self.meta("botA", UID), ["[日常] 在家做饭"], ids))
        self.assertEqual(out["archived"], 1)
        self.assertEqual(self.status(here["memory_id"]), "archived")
        self.assertEqual(self.status(there["memory_id"]), "active")


if __name__ == "__main__":
    unittest.main()
