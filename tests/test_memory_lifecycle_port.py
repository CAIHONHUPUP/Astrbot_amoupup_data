# -*- coding: utf-8 -*-
"""记忆生命周期（取代/归档/周整理）单测，从 repo 的 test_memory_lifecycle.py 迁到新布局。"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import Stack  # noqa: E402

BOT = "bot1"
UID = "302578963"


class SupersedeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-life-")
        self.stack = Stack(self.tmp, "amo.db")

    def tearDown(self):
        self.stack.close()

    def _status(self, mid):
        row = self.stack.conn.execute(
            "SELECT status, superseded_by FROM memories WHERE id=?", (mid,)).fetchone()
        return row["status"], row["superseded_by"]

    def test_new_home_supersedes_old(self):
        m = self.stack.memory
        old = m.remember(BOT, UID, "主人住在东莞东兴路一带。", importance=0.7)
        new = m.remember(BOT, UID, "主人搬到深圳南山住了。", importance=0.7)
        self.assertEqual(new["superseded"], [old["memory_id"]])
        self.assertEqual(self._status(old["memory_id"])[0], "superseded")
        rows = self.stack.retrieval.recall(BOT, UID, "主人住在哪")
        self.assertTrue(rows)
        self.assertIn("深圳", rows[0]["content"])
        self.assertFalse(any("东兴路" in r["content"] for r in rows))

    def test_unrelated_fact_does_not_supersede(self):
        m = self.stack.memory
        old = m.remember(BOT, UID, "主人住在东莞东兴路。", importance=0.7)
        new = m.remember(BOT, UID, "主人喜欢喝美式咖啡。", importance=0.5)
        self.assertEqual(new["superseded"], [])
        self.assertEqual(self._status(old["memory_id"])[0], "active")

    def test_same_value_not_superseded(self):
        m = self.stack.memory
        a = m.remember(BOT, UID, "主人住在东莞东兴路。")
        b = m.remember(BOT, UID, "他住在东莞东兴路那边。")
        self.assertEqual(b["superseded"], [])
        self.assertEqual(self._status(a["memory_id"])[0], "active")

    def test_manual_supersede(self):
        m = self.stack.memory
        a = m.remember(BOT, UID, "主人养了一只猫。")
        b = m.remember(BOT, UID, "主人养了一只狗。")
        self.assertTrue(m.mark_superseded(a["memory_id"], b["memory_id"]))
        self.assertEqual(self._status(a["memory_id"])[0], "superseded")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-arch-")
        self.stack = Stack(self.tmp, "amo.db")

    def tearDown(self):
        self.stack.close()

    def test_archive_stale_only(self):
        old = int(time.time()) - 200 * 86400
        m = self.stack.memory
        stale = m.remember(BOT, UID, "很久以前的一件小事", importance=0.2,
                           created_at=old, last_hit_at=old)
        m.remember(BOT, UID, "最近的重要事情，生日是十二月十号", importance=0.9)
        result = self.stack.compression.archive_stale(BOT, days=90, max_importance=0.3)
        self.assertEqual(result["archived"], 1)
        status = self.stack.conn.execute(
            "SELECT status FROM memories WHERE id=?", (stale["memory_id"],)).fetchone()["status"]
        self.assertEqual(status, "archived")
        rows = self.stack.retrieval.recall(BOT, UID, "很久以前的小事")
        self.assertFalse(any("很久以前的一件小事" in r["content"] for r in rows))
        self.assertTrue(self.stack.retrieval.recall(BOT, UID, "生日"))

    def test_archive_dry_run_changes_nothing(self):
        old = int(time.time()) - 200 * 86400
        item = self.stack.memory.remember(BOT, UID, "很旧的小事", importance=0.2,
                                          created_at=old, last_hit_at=old)
        result = self.stack.compression.archive_stale(BOT, days=90, max_importance=0.3,
                                                      apply=False)
        self.assertEqual(result["candidate"], 1)
        self.assertEqual(result["archived"], 0)
        row = self.stack.conn.execute(
            "SELECT status FROM memories WHERE id=?", (item["memory_id"],)).fetchone()
        self.assertEqual(row["status"], "active")


class CompressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-comp-")
        # 关掉写入时的自动去重，专门测"周度整理"这一步
        self.stack = Stack(self.tmp, "amo.db", settings={"auto_dedupe": False})

    def tearDown(self):
        self.stack.close()

    def test_same_day_summaries_merge(self):
        m = self.stack.memory
        a = m.remember(BOT, UID, "2026-11-08 下午和主人聊了工作。", kind="conversation_summary")
        b = m.remember(BOT, UID, "2026-11-08 晚上和主人聊了天气和晚饭。", kind="conversation_summary")
        result = self.stack.compression.compress_memories(BOT, apply=True)
        self.assertEqual(result["merged_summaries"], 1)
        statuses = [
            self.stack.conn.execute("SELECT status FROM memories WHERE id=?", (item["memory_id"],))
            .fetchone()["status"]
            for item in (a, b)
        ]
        self.assertIn("merged", statuses)
        self.assertIn("active", statuses)

    def test_near_duplicate_merge(self):
        m = self.stack.memory
        a = m.remember(BOT, UID, "主人的工作作息是中午有休息时间，下午五点半下班。")
        b = m.remember(BOT, UID, "主人的工作作息是中午有休息时间，下午五点半下班啦。")
        result = self.stack.compression.compress_memories(BOT, apply=True)
        self.assertGreaterEqual(result["merged_near_duplicates"], 1)
        statuses = [
            self.stack.conn.execute("SELECT status FROM memories WHERE id=?", (item["memory_id"],))
            .fetchone()["status"]
            for item in (a, b)
        ]
        self.assertIn("merged", statuses)

    def test_information_rich_pair_not_merged(self):
        """一条是另一条的子集时不能合并，否则会丢信息。"""
        m = self.stack.memory
        a = m.remember(BOT, UID, "主人的工作作息是中午有休息时间，下午五点半下班。")
        b = m.remember(BOT, UID, "主人的作息是中午休息一会儿，下午五点半就下班回家。")
        result = self.stack.compression.compress_memories(BOT, apply=True)
        self.assertEqual(result["merged_near_duplicates"], 0)
        statuses = {
            self.stack.conn.execute("SELECT status FROM memories WHERE id=?", (item["memory_id"],))
            .fetchone()["status"]
            for item in (a, b)
        }
        self.assertEqual(statuses, {"active"})

    def test_status_counts_available(self):
        """老测试用 store.stats()；新布局里状态统计走 SQL（面板自己拼）。"""
        self.stack.memory.remember(BOT, UID, "一条普通记忆")
        row = self.stack.conn.execute(
            "SELECT SUM(CASE WHEN COALESCE(status,'active')='active' THEN 1 ELSE 0 END) AS active,"
            " SUM(CASE WHEN status='archived' THEN 1 ELSE 0 END) AS archived FROM memories "
            "WHERE bot_id=?", (BOT,)).fetchone()
        self.assertGreaterEqual(int(row["active"] or 0), 1)
        self.assertEqual(int(row["archived"] or 0), 0)


if __name__ == "__main__":
    unittest.main()
