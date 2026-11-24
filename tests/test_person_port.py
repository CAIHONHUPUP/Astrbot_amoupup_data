# -*- coding: utf-8 -*-
"""人物/跨平台档案单测，从 repo 的 test_person.py 迁到新布局。"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import Stack  # noqa: E402
from models.memory_db import MemoryDB  # noqa: E402
from services.person_engine import PersonEngine  # noqa: E402
from utils.text_process import platform_label, platform_of_umo  # noqa: E402


class PersonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-person-")
        self.stack = Stack(self.tmp, "amo_data.db")
        self.bot = "bot1"

    def tearDown(self):
        self.stack.close()

    def test_platform_label(self):
        self.assertEqual(platform_label("aiocqhttp"), "QQ")
        self.assertEqual(platform_label("lark"), "飞书")
        self.assertEqual(platform_of_umo("aiocqhttp:GroupMessage:123"), "aiocqhttp")
        self.assertEqual(platform_of_umo("lark:FriendMessage:ou_abc"), "lark")

    def test_same_name_auto_merge(self):
        p = self.stack.persons
        qq_pid = p.resolve_person(self.bot, "aiocqhttp", "10001", "墨辞烟")
        lark_pid = p.resolve_person(self.bot, "lark", "ou_abc", "墨辞烟")
        self.assertEqual(qq_pid, lark_pid)
        links = p.person_links(self.bot, qq_pid)
        self.assertEqual(len(links), 2)
        self.assertEqual({item["label"] for item in links}, {"QQ", "飞书"})

    def test_different_name_not_merged(self):
        p = self.stack.persons
        a = p.resolve_person(self.bot, "aiocqhttp", "10001", "小明")
        b = p.resolve_person(self.bot, "lark", "ou_abc", "小红")
        self.assertNotEqual(a, b)

    def test_ambiguous_name_not_merged(self):
        p = self.stack.persons
        p.resolve_person(self.bot, "aiocqhttp", "10001", "小明")
        p.resolve_person(self.bot, "aiocqhttp", "10002", "小明")
        before = len(p.list_persons(self.bot))
        p.resolve_person(self.bot, "lark", "ou_abc", "小明")
        self.assertEqual(len(p.list_persons(self.bot)), before + 1)

    def test_messages_carry_platform_and_pid(self):
        s = self.stack
        s.messages.add_message(self.bot, "aiocqhttp:FriendMessage:10001", "10001",
                               "墨辞烟", "user", "在吗")
        s.messages.add_message(self.bot, "lark:FriendMessage:ou_abc", "ou_abc",
                               "墨辞烟", "user", "飞书也来了")
        rows = s.messages.list_messages(self.bot, limit=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["platform"] for row in rows}, {"aiocqhttp", "lark"})
        self.assertEqual({row["label"] for row in rows}, {"QQ", "飞书"})
        pids = {row["pid"] for row in rows}
        self.assertEqual(len(pids), 1)
        self.assertGreater(list(pids)[0], 0)

    def test_person_context_returns_other_platform(self):
        s = self.stack
        s.messages.add_message(self.bot, "aiocqhttp:FriendMessage:10001", "10001",
                               "墨辞烟", "user", "今天加班")
        s.messages.add_message(self.bot, "lark:FriendMessage:ou_abc", "ou_abc",
                               "墨辞烟", "user", "明天出差")
        detail = s.persons.person_context(self.bot, "aiocqhttp", "10001", 10)
        self.assertTrue(detail)
        self.assertEqual(len(detail["links"]), 2)
        self.assertEqual(len(detail["others"]), 1)
        self.assertEqual(detail["others"][0]["label"], "飞书")
        texts = " ".join(item["content"] for item in detail["recent"])
        self.assertIn("明天出差", texts)

    def test_merge_and_unlink(self):
        p = self.stack.persons
        a = p.resolve_person(self.bot, "aiocqhttp", "10001", "阿墨")
        b = p.resolve_person(self.bot, "lark", "ou_abc", "阿墨仔")
        self.assertNotEqual(a, b)
        self.assertTrue(p.merge_persons(self.bot, a, b))
        detail = p.person_detail(self.bot, a)
        self.assertEqual(len(detail["links"]), 2)
        self.assertFalse(p.person_detail(self.bot, b))
        self.assertTrue(p.unlink_identity(self.bot, "lark", "ou_abc"))
        detail = p.person_detail(self.bot, a)
        self.assertEqual(len(detail["links"]), 1)

    def test_list_persons_counts_messages(self):
        s = self.stack
        s.messages.add_message(self.bot, "aiocqhttp:FriendMessage:10001", "10001",
                               "阿墨", "user", "1")
        s.messages.add_message(self.bot, "aiocqhttp:FriendMessage:10001", "10001",
                               "阿墨", "user", "2")
        items = s.persons.list_persons(self.bot)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["msg_count"], 2)
        self.assertEqual(items[0]["platforms"], ["QQ"])

    def test_find_profile_across_bots(self):
        prof = self.stack.profiles
        prof.save_profile("botA", "10001", name="阿墨", relation="相恋")
        found = prof.find_profile("10001")
        self.assertEqual(found["bot_id"], "botA")
        self.assertEqual(found["name"], "阿墨")
        self.assertEqual(prof.find_profile("不存在"), {})

    def test_list_messages_filter_by_uid(self):
        s = self.stack
        s.messages.add_message(self.bot, "aiocqhttp:FriendMessage:1", "10001", "甲", "user", "a")
        s.messages.add_message(self.bot, "aiocqhttp:FriendMessage:1", "10002", "乙", "user", "b")
        rows = s.messages.list_messages(self.bot, uid="10001")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "a")

    def test_old_db_gets_platform_columns(self):
        # 模拟老库：建表时没有 platform / pid，打开时自动补列
        path = os.path.join(self.tmp, "old.db")
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, "
            "umo TEXT NOT NULL, uid TEXT DEFAULT '', name TEXT DEFAULT '', "
            "role TEXT DEFAULT 'user', content TEXT NOT NULL, created_at INTEGER NOT NULL, "
            "compressed INTEGER DEFAULT 0)")
        conn.execute(
            "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at) "
            "VALUES('bot1','aiocqhttp:FriendMessage:1','1','阿墨','user','旧消息',1)")
        conn.commit()
        conn.close()

        db = MemoryDB(path, {})
        persons = PersonEngine(db, {}, None)
        messages = __import__(
            "services.message_engine", fromlist=["MessageEngine"]).MessageEngine(db, {}, persons)
        try:
            rows = messages.list_messages("bot1", limit=10)
            self.assertEqual(rows[0]["content"], "旧消息")
            self.assertEqual(rows[0]["platform"], "")
            messages.add_message("bot1", "aiocqhttp:FriendMessage:1", "1", "阿墨", "user", "新消息")
            self.assertEqual(len(messages.list_messages("bot1", limit=10)), 2)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
