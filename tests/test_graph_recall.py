# -*- coding: utf-8 -*-
"""实体/关系路召回：名字、别名、关系词 → 按 pid 取那个人的记忆。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from support.store_fixture import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"
COUSIN = "cousin1"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class EntityRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"), settings={
            "embedding_enabled": False, "auto_dedupe": False,
        })
        now = int(time.time())
        with self.store.lock:
            conn = self.store.conn
            conn.execute(
                "INSERT OR REPLACE INTO persons(pid,bot_id,name,created_at,updated_at) "
                "VALUES(?,?,?,?,?)", (2, BOT, "小周", now, now))
            conn.execute(
                "INSERT OR REPLACE INTO person_links(bot_id,platform,uid,pid,name,created_at) "
                "VALUES(?,?,?,?,?,?)", (BOT, "aiocqhttp", COUSIN, 2, "小周", now))
            conn.execute(
                "INSERT OR REPLACE INTO relations(bot_id,a_uid,b_uid,relation,note,updated_at) "
                "VALUES(?,?,?,?,?,?)", (BOT, UID, COUSIN, "表哥", "", now))
            conn.execute(
                "INSERT INTO memories(bot_id,uid,pid,platform,scope,group_id,kind,subject,"
                "content,importance,created_at,last_hit_at,status,tags,hit_count) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (BOT, UID, 2, "aiocqhttp", "private", "", "fact", "other",
                 "小周最近转了行，在做建筑设计", 0.7, now, now, "active", ""))
            conn.commit()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_relation_word_finds_person_memory(self):
        rows = self.store.recall(BOT, UID, "我表哥最近怎么样")
        self.assertTrue(rows, "关系词「表哥」应该认出关系另一头的人")
        self.assertIn("建筑设计", rows[0]["content"])

    def test_name_finds_person_memory(self):
        rows = self.store.recall(BOT, UID, "小周最近在忙什么")
        self.assertTrue(rows)
        self.assertIn("建筑设计", rows[0]["content"])

    def test_disabling_route_reverts(self):
        self.store.settings["recall_graph_route"] = False
        self.assertEqual(self.store.recall(BOT, UID, "我表哥最近怎么样"), [],
                         "关掉实体路就该回到纯文本召回（想不起来）")

    def test_unrelated_query_stays_empty(self):
        self.assertEqual(self.store.recall(BOT, UID, "外面下雨了吗"), [])

    def test_entity_tag_finds_pidless_memory(self):
        """老数据 pid=0，但写的时候挂了 entity:小周 → 按名字也能捞到。"""
        now = int(time.time())
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO memories(bot_id,uid,pid,platform,scope,group_id,kind,subject,"
                "content,importance,created_at,last_hit_at,status,tags,hit_count) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (BOT, UID, 0, "", "private", "", "event", "user",
                 "他今天又加班到很晚", 0.5, now, now, "active", "entity:小周"))
            self.store.conn.commit()
        rows = self.store.recall(BOT, UID, "小周最近怎么样")
        self.assertTrue(any("加班" in row["content"] for row in rows),
                        "entity 标签应该把 pid=0 的回忆也带进来")


if __name__ == "__main__":
    unittest.main()
