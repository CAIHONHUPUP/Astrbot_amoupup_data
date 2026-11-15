# -*- coding: utf-8 -*-
"""夜间提炼契约：能记侧必须给守护插件提供候选流水与写回/归档。

2026-11-15 之前这条链路是断的：守护 03:00 调 nightly_memory_candidates，
能记根本没有这两个方法，日志里每天都是「记忆插件不可用，跳过」。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from main import MemoryAPI  # noqa: E402
from support.store_fixture import DataStore  # noqa: E402

BOT = "大肥鱼"
UID = "302578963"


class NightlyMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="amo-data-nightly-")
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"),
                               {"embedding_enabled": False, "embedding_backfill": False})
        self.api = MemoryAPI(self.store)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass

    def run_api(self, coro):
        return asyncio.run(coro)

    def write(self, text, **kwargs):
        return self.store.memory.remember(BOT, UID, text, **kwargs)

    def candidates(self):
        return self.run_api(self.api.nightly_memory_candidates(hours=24, limit=50))

    def meta(self):
        return {"bot_id": BOT, "uid": UID, "scope": "private",
                "group_id": "", "subject": "user"}

    def test_candidates_returns_recent_active_rows(self) -> None:
        self.write("爱吃螺蛳粉", kind="fact")
        rows = self.candidates()
        self.assertTrue(rows)
        for key in ("id", "bot_id", "uid", "scope", "group_id", "subject", "content"):
            self.assertIn(key, rows[0])
        self.assertEqual(rows[0]["bot_id"], BOT)
        self.assertEqual(rows[0]["scope"], "private")

    def test_candidates_skip_old_rows(self) -> None:
        old = int(time.time()) - 48 * 3600
        self.write("三天前说过的话", kind="fact", created_at=old)
        self.write("刚刚说的话", kind="fact")
        contents = [row["content"] for row in self.candidates()]
        self.assertIn("刚刚说的话", contents)
        self.assertNotIn("三天前说过的话", contents)

    def test_apply_writes_facts_and_archives(self) -> None:
        self.write("今天去了动物园看熊猫", kind="fact")
        self.write("熊猫吃竹子很可爱", kind="fact")
        ids = [row["id"] for row in self.candidates()]
        out = self.run_api(self.api.nightly_apply(
            self.meta(), ["[出行] 去了动物园看熊猫"], ids))
        self.assertEqual(out["written"], 1)
        self.assertGreaterEqual(out["archived"], 1)
        left = self.candidates()
        left_ids = {row["id"] for row in left}
        self.assertTrue(left_ids & set(ids) or any("动物园" in row["content"] for row in left),
                        "提炼后的事实应该还能被召回")
        self.assertLess(len(left), len(ids) + 1, "原流水应该被归档掉")

    def test_apply_without_facts_keeps_streams(self) -> None:
        self.write("随便聊聊", kind="fact")
        ids = [row["id"] for row in self.candidates()]
        out = self.run_api(self.api.nightly_apply(self.meta(), [], ids))
        self.assertEqual(out, {"written": 0, "archived": 0})
        self.assertTrue({row["id"] for row in self.candidates()} >= set(ids),
                        "一条都没提炼出来时不能归档")

    def test_merged_fact_is_not_archived(self) -> None:
        """提炼结果和某条流水几乎一样时会被合并进那一行，这行不能再归档。"""
        self.write("爱吃螺蛳粉", kind="fact")
        ids = [row["id"] for row in self.candidates()]
        out = self.run_api(self.api.nightly_apply(self.meta(), ["爱吃螺蛳粉"], ids))
        self.assertEqual(out["written"], 1)
        self.assertEqual(out["archived"], 0, "被合并的那行要是归档了，事实就跟着消失了")
        self.assertTrue([row for row in self.candidates() if "螺蛳粉" in row["content"]])

    def test_archive_ignores_bad_ids(self) -> None:
        self.assertEqual(self.store.memory.nightly_archive([None, "x", 0]), 0)
        self.assertEqual(self.store.memory.nightly_archive([]), 0)


if __name__ == "__main__":
    unittest.main()
