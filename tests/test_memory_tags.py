# -*- coding: utf-8 -*-
"""B2：记忆标签（tags）真落库、同 idem 幂等、迁移可重复（2026-11-16）。"""
from __future__ import annotations

import asyncio
import os
import sqlite3
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
TAGS = "#活动"
IDEM = "activity:2026-11-16:14:00"


class TagsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="amo-data-tags-")
        self.path = os.path.join(self.tmp, "amo_data.db")
        self.store = DataStore(self.path, {"embedding_enabled": False,
                                           "embedding_backfill": False})

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:  # 关库失败不该把断言结果盖掉
            pass

    def rows(self):
        return self.store.memory.list_memories(BOT, UID, 20)


class RememberTagsTests(TagsCase):
    def test_remember_persists_tags_and_decimal_importance(self) -> None:
        self.store.memory.remember(BOT, UID, "和柳如烟逛街", kind="activity",
                                   importance=0.25, tags=TAGS)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], TAGS)
        self.assertAlmostEqual(float(rows[0]["importance"]), 0.25, places=4)

    def test_positional_legacy_call_still_works(self) -> None:
        """tags 追加在参数表末尾：按位置传参的老调用不能被破坏。"""
        api = MemoryAPI(self.store)
        result = asyncio.run(api.remember(BOT, UID, "和柳如烟逛街", "activity", 0.25))
        self.assertNotEqual(result.get("ok"), False)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], "")

    def test_same_idem_key_keeps_one_row_and_touch_sets_tags(self) -> None:
        self.store.memory.remember(BOT, UID, "和柳如烟逛街", kind="activity",
                                   importance=0.25, idem_key=IDEM)
        self.store.memory.remember(BOT, UID, "和柳如烟逛街", kind="activity",
                                   importance=0.25, tags=TAGS, idem_key=IDEM)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], TAGS)

    def test_merge_path_keeps_tags(self) -> None:
        self.store.memory.remember(BOT, UID, "我喜欢喝美式咖啡", kind="fact",
                                   importance=0.25)
        self.store.memory.remember(BOT, UID, "我喜欢喝美式咖啡。", kind="fact",
                                   importance=0.25, tags=TAGS)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], TAGS)
        self.assertIn("。", rows[0]["content"])

    def test_revive_path_keeps_tags(self) -> None:
        wrote = self.store.memory.remember(BOT, UID, "他喜欢喝无糖茶", kind="fact",
                                           importance=0.25, idem_key="fact:tea")
        self.assertTrue(self.store.memory.dao.mark_superseded(int(wrote["memory_id"]), 0))
        self.store.memory.remember(BOT, UID, "他喜欢喝无糖茶", kind="fact",
                                   importance=0.25, tags=TAGS, idem_key="fact:tea")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], TAGS)

    def test_api_remember_forwards_tags(self) -> None:
        api = MemoryAPI(self.store)
        result = asyncio.run(api.remember(BOT, UID, "和柳如烟逛街", kind="activity",
                                          importance=0.25, tags=TAGS, idem_key=IDEM))
        self.assertNotEqual(result.get("ok"), False)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], TAGS)

    def test_empty_tags_do_not_clear_existing_tags(self) -> None:
        """同一 idem 不带 tags 再写一次：老标签必须留着（空值不覆盖，CASE WHEN 兜住）。"""
        self.store.memory.remember(BOT, UID, "和柳如烟逛街", kind="activity",
                                   importance=0.25, tags=TAGS, idem_key=IDEM)
        self.store.memory.remember(BOT, UID, "和柳如烟逛街", kind="activity",
                                   importance=0.25, idem_key=IDEM)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], TAGS)

    def test_tags_are_normalized_and_capped(self) -> None:
        """tags 清洗：折叠空白、去首尾、截到 200 字（太长会把库撑胖）。"""
        dirty = "  #活动   #咖啡  " + "长" * 300
        self.store.memory.remember(BOT, UID, "和柳如烟逛街", kind="activity",
                                   importance=0.25, tags=dirty)
        got = self.rows()[0]["tags"]
        self.assertTrue(got.startswith("#活动 #咖啡"), got[:20])
        self.assertLessEqual(len(got), 200)

    def test_null_tags_reads_as_empty_string(self) -> None:
        """外部/旧快照写进来的 NULL tags 不能把消费方读崩：列表接口统一给空串。"""
        self.store.memory.remember(BOT, UID, "和柳如烟逛街", kind="activity",
                                   importance=0.25)
        self.store.conn.execute("UPDATE memories SET tags=NULL")
        self.store.conn.commit()
        self.assertEqual(self.rows()[0]["tags"], "")

    def test_revive_without_tags_keeps_tags(self) -> None:
        """复活（同一 idem 的旧行被取代后又写一遍）不带 tags 时也要保留旧标签。"""
        wrote = self.store.memory.remember(BOT, UID, "他喜欢喝无糖茶", kind="fact",
                                           importance=0.25, tags=TAGS, idem_key="fact:tea")
        self.assertTrue(self.store.memory.dao.mark_superseded(int(wrote["memory_id"]), 0))
        self.store.memory.remember(BOT, UID, "他喜欢喝无糖茶", kind="fact",
                                   importance=0.25, idem_key="fact:tea")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], TAGS)


class IdemRaceTests(TagsCase):
    def test_insert_race_falls_back_to_merge(self) -> None:
        """唯一索引竞态：查询没看到、INSERT 撞 UNIQUE 时必须并回原行，不能丢记忆。"""
        self.store.memory.remember(BOT, UID, "和柳如烟逛街", kind="activity",
                                   importance=0.25, idem_key=IDEM)
        dao = self.store.memory.dao
        calls = {"n": 0}
        active, any_state = dao.find_active_by_idem, dao.find_by_idem

        def miss_twice(fn):
            def wrapped(*args, **kwargs):
                calls["n"] += 1
                return None if calls["n"] <= 2 else fn(*args, **kwargs)
            return wrapped

        dao.find_active_by_idem = miss_twice(active)
        dao.find_by_idem = miss_twice(any_state)
        try:
            merged = self.store.memory.remember(BOT, UID, "和柳如烟逛街", kind="activity",
                                                importance=0.3, idem_key=IDEM)
        finally:
            dao.find_active_by_idem = active
            dao.find_by_idem = any_state
        self.assertTrue(merged.get("ok"), merged)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(float(rows[0]["importance"]), 0.3)


class TagsMigrationTests(unittest.TestCase):
    def _legacy_db(self) -> str:
        """老库：memories 表没有 tags 列。"""
        tmp = tempfile.mkdtemp(prefix="amo-data-tags-mig-")
        path = os.path.join(tmp, "old.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE memories ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, uid TEXT NOT NULL, "
            "scope TEXT NOT NULL DEFAULT 'private', group_id TEXT DEFAULT '', "
            "kind TEXT DEFAULT 'fact', content TEXT NOT NULL, importance REAL DEFAULT 0.5, "
            "hit_count INTEGER DEFAULT 0, created_at INTEGER NOT NULL, "
            "last_hit_at INTEGER NOT NULL, idem_key TEXT DEFAULT '');"
        )
        conn.execute(
            "INSERT INTO memories(bot_id, uid, content, created_at, last_hit_at) "
            "VALUES('bot', '旧人', '旧记忆', 1, 1)"
        )
        conn.commit()
        conn.close()
        return path

    def test_new_schema_ships_tags_column(self) -> None:
        """SCHEMA_SQL 建的新库自带 tags：不能只靠 ALTER 补列（补列失败=全库写入报错）。"""
        from models.memory_db import SCHEMA_SQL
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(SCHEMA_SQL)
            info = {row[1]: row[4] for row in conn.execute("PRAGMA table_info(memories)")}
        finally:
            conn.close()
        self.assertIn("tags", info)
        self.assertEqual(str(info["tags"]), "''")

    def test_legacy_db_gets_tags_and_reopen_is_idempotent(self) -> None:
        path = self._legacy_db()
        settings = {"embedding_enabled": False, "embedding_backfill": False}
        first = DataStore(path, settings)
        try:
            first.memory.remember(BOT, UID, "旧库补标签", kind="fact",
                                  importance=0.25, tags=TAGS)
            first_rows = first.memory.list_memories(BOT, UID, 10)
        finally:
            first.close()
        self.assertEqual(len(first_rows), 1)
        self.assertEqual(first_rows[0]["tags"], TAGS)

        second = DataStore(path, settings)
        try:
            cols = [row["name"] for row in
                    second.conn.execute("PRAGMA table_info(memories)").fetchall()]
            rows = second.memory.list_memories(BOT, UID, 10)
            legacy = second.conn.execute(
                "SELECT tags FROM memories WHERE uid='旧人'").fetchone()
        finally:
            second.close()
        self.assertEqual(cols.count("tags"), 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tags"], TAGS)
        # 老行补列后的默认值必须是空串，不能是 NULL（NULL 会让消费方 split 崩）
        self.assertEqual(str(legacy["tags"]), "")
