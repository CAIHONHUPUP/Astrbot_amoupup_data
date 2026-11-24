# -*- coding: utf-8 -*-
"""时间回看接口单测（从 repo 的 test_time_recall_store.py 迁到新布局）。

老 `store.DataStore` → 新 `services.message_engine.MessageEngine` + 临时库。
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from utils import clock  # noqa: E402
from services.message_engine import MessageEngine  # noqa: E402

BOT = "bot1"
UID = "10001"
UMO = "aiocqhttp:FriendMessage:10001"

_DDL = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL, umo TEXT NOT NULL, uid TEXT DEFAULT '',
    name TEXT DEFAULT '', role TEXT DEFAULT 'user', content TEXT NOT NULL,
    created_at INTEGER NOT NULL, compressed INTEGER DEFAULT 0,
    platform TEXT DEFAULT '', pid INTEGER DEFAULT 0,
    message_id TEXT DEFAULT '', group_name TEXT DEFAULT ''
);
"""


class _DB:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript(_DDL)
        self.conn.commit()


class _Persons:
    def resolve_person(self, *a, **k):
        return 0

    def canonical_uid(self, bot_id, platform, uid):
        return uid

    def canonical_uid_for_uid(self, bot_id, uid):
        return uid


class _Host:
    persons = _Persons()


def _ts(moment):
    zone = clock.get_zone("Asia/Shanghai")
    return int(moment.replace(tzinfo=zone).timestamp()) if zone else int(moment.timestamp())


class TimeWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-recall-")
        self.db = _DB(os.path.join(self.tmp, "chat.db"))
        self.engine = MessageEngine(self.db, {}, _Persons())

    def tearDown(self):
        self.db.conn.close()

    def test_yesterday_whole_day(self):
        today = clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = MessageEngine._time_window("昨天")
        self.assertEqual(start, _ts(today - timedelta(days=1)))
        self.assertEqual(end, _ts(today))

    def test_yesterday_afternoon_is_narrowed(self):
        yesterday = clock.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        start, end = MessageEngine._time_window("昨天下午")
        self.assertEqual(start, _ts(yesterday + timedelta(hours=13)))
        self.assertEqual(end, _ts(yesterday + timedelta(hours=18)))

    def test_last_night(self):
        today = clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = MessageEngine._time_window("昨晚我们聊了什么")
        self.assertEqual(start, _ts(today - timedelta(days=1) + timedelta(hours=18)))
        self.assertEqual(end, _ts(today))
        self.assertEqual(MessageEngine._time_label("昨晚我们聊了什么"), "昨晚")

    def test_last_weekday(self):
        today = clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
        monday = today - timedelta(days=today.weekday())
        start, end = MessageEngine._time_window("还记得上周三我说的事吗")
        self.assertEqual(end - start, 86400)
        self.assertEqual(
            datetime.fromtimestamp(start, clock.get_zone(clock.current_name())).weekday(), 2)
        self.assertLess(start, _ts(monday))

    def test_recent_days(self):
        # 2026-11-24 口径统一后：「最近3天」= 从现在往前滚动 72 小时（与能陪回看一致），
        # 不再是「今天+昨天+前天」三个自然日。
        today = clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = MessageEngine._time_window("最近3天我提过什么")
        self.assertAlmostEqual((end - start) / 86400.0, 3.0, places=0)
        self.assertLessEqual(start, _ts(today))
        self.assertGreater(start, _ts(today - timedelta(days=4)))

    def test_unknown_returns_zero(self):
        self.assertEqual(MessageEngine._time_window("你在干嘛"), (0, 0))

    def test_label_matches_window(self):
        self.assertEqual(MessageEngine._time_label("昨天下午我们聊了啥"), "昨天下午")
        self.assertEqual(MessageEngine._time_label("最近3天我提过什么"), "最近3天")
        self.assertTrue(MessageEngine._time_label("还记得上周三我说的事吗").startswith("上周三"))


class RecallMessagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-recall-")
        self.db = _DB(os.path.join(self.tmp, "chat.db"))
        self.engine = MessageEngine(self.db, {}, _Persons())
        self.engine.plugin = _Host()
        today0 = clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday0 = today0 - timedelta(days=1)
        self._add("昨天的午饭", _ts(yesterday0 + timedelta(hours=11)))
        self._add("昨天下午的班", _ts(yesterday0 + timedelta(hours=14)))
        self._add("今天的消息", _ts(today0 + timedelta(hours=9)))

    def tearDown(self):
        self.db.conn.close()

    def _add(self, text, created_at, bot=BOT):
        mid = self.engine.add_message(bot, UMO, UID, "他", "user", text, created_at=created_at)
        with self.engine.lock:
            self.engine.conn.execute("UPDATE messages SET created_at=? WHERE id=?",
                                     (created_at, mid))
            self.engine.conn.commit()

    def test_recall_only_returns_window_and_ascending(self):
        rows = self.engine.recall_messages(BOT, query="昨天", uid=UID)
        self.assertEqual([r["content"] for r in rows], ["昨天的午饭", "昨天下午的班"])

    def test_recall_afternoon_filters_morning(self):
        rows = self.engine.recall_messages(BOT, query="昨天下午", uid=UID)
        self.assertEqual([r["content"] for r in rows], ["昨天下午的班"])

    def test_recall_unknown_time_returns_empty(self):
        self.assertEqual(self.engine.recall_messages(BOT, query="你在干嘛", uid=UID), [])

    def test_recall_respects_limit(self):
        rows = self.engine.recall_messages(BOT, query="昨天", uid=UID, limit=1)
        self.assertEqual([r["content"] for r in rows], ["昨天下午的班"])

    def test_recall_isolated_by_bot(self):
        self._add("别人的消息", int(time.time()) - 86400, bot=BOT)
        self.assertEqual(self.engine.recall_messages("bot2", query="昨天", uid=UID), [])

    def test_messages_since(self):
        since = _ts(clock.now().replace(hour=0, minute=0, second=0, microsecond=0))
        rows = self.engine.messages_since(BOT, uid=UID, since_ts=since)
        self.assertEqual([r["content"] for r in rows], ["今天的消息"])


if __name__ == "__main__":
    unittest.main()
