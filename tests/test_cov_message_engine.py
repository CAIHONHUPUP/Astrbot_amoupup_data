# -*- coding: utf-8 -*-
"""覆盖率补齐：services/message_engine.py 的时间解析与降级分支。"""
from __future__ import annotations

import datetime as dt
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from services import message_engine as me
from services.message_engine import MessageEngine


def _today(y=2026, m=11, d=18, hour=0):
    return dt.datetime(y, m, d, hour)


class TimeWindowTests(unittest.TestCase):
    def test_month_day_falls_back_to_last_year_and_can_fail(self) -> None:
        # 2028 是闰年：2 月 29 今年有、去年没有 -> 回落失败返回 None
        self.assertIsNone(MessageEngine._tw_month_day(
            "2月29日", _today(2028, 1, 15), _today(2028, 1, 15, 12)))
        # 正常回落：12 月 31 日今年还没到 -> 用去年
        window = MessageEngine._tw_month_day("12月31日", _today(2026, 11, 18),
                                             _today(2026, 11, 18, 12))
        self.assertIsNotNone(window)
        self.assertLess(window[0], window[1])

    def test_day_num_rejects_out_of_range_and_prev_month(self) -> None:
        self.assertIsNone(MessageEngine._tw_day_num("32号", _today(), _today(2026, 11, 18, 12)))
        # 3 月 30 日说「31 号」：本月有 31，但上个月（2 月）没有 -> None
        self.assertIsNone(MessageEngine._tw_day_num(
            "31号", dt.datetime(2026, 3, 30), dt.datetime(2026, 3, 30, 12)))

    def test_absolute_and_recent_windows(self) -> None:
        today = _today()
        now = _today(2026, 11, 18, 15)
        last_year = MessageEngine._tw_absolute("去年", today, now)
        this_year = MessageEngine._tw_absolute("今年", today, now)
        self.assertLess(last_year[0], last_year[1])
        self.assertLess(this_year[0], this_year[1])
        recent = MessageEngine._tw_recent("最近3小时", today, now)
        self.assertLess(recent[0], recent[1])
        self.assertIsNone(MessageEngine._tw_absolute("没有时间词", today, now))

    def test_time_label_words_and_empty(self) -> None:
        self.assertEqual(MessageEngine._time_label("上个月"), "上个月")
        self.assertEqual(MessageEngine._time_label("最近5天"), "最近5天")
        self.assertEqual(MessageEngine._time_label("随便聊聊"), "")


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    @property
    def rowcount(self):
        return len(self.rows)

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _FakeConn:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.sql = []
        self.params = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self.params.append(list(params or []))
        return _FakeCursor(self.rows)

    def commit(self):
        return None


def _engine(conn=None, rows=None):
    db = SimpleNamespace(conn=conn or _FakeConn(rows), lock=threading.Lock())
    engine = MessageEngine(db, settings={}, persons=None)
    engine.plugin = SimpleNamespace(persons=SimpleNamespace(
        canonical_uid_for_uid=_raising_uid))
    return engine


def _raising_uid(*args, **kwargs):
    raise RuntimeError("uid 归一化炸了")


class RecallFallbackTests(unittest.TestCase):
    def test_recall_messages_survives_person_error(self) -> None:
        engine = _engine()
        self.assertEqual(engine.recall_messages("b", query=""), [])

    def test_recall_by_time_survives_span_error(self) -> None:
        engine = _engine()
        with mock.patch.object(me, "_parse_time_span",
                                        side_effect=ValueError("解析炸了")):
            out = engine.recall_by_time("b", query="昨天")
        self.assertEqual(out, {"messages": [], "span": None, "path": {}, "label": ""})

    def test_group_overrides_keeps_raw_non_json_value(self) -> None:
        conn = _FakeConn([{"key": "group_name:qq:1", "value": "{不是 json"}])
        engine = _engine(conn)
        self.assertEqual(engine.group_name_overrides("b"), {"qq:1": "{不是 json"})

    def test_list_rows_applies_pid_and_platform_filters(self) -> None:
        conn = _FakeConn([])
        engine = _engine(conn)
        rows = engine._list_rows("b", "qq:1", 10, pid=5, platform="qq",
                                 uid="u", role="chat")
        self.assertEqual(rows, [])
        sql = conn.sql[0]
        self.assertIn("AND pid=?", sql)
        self.assertIn("AND platform=?", sql)
        self.assertEqual(conn.params[0][-3:], ["qq", "u", 10])
        self.assertIn(5, conn.params[0])


if __name__ == "__main__":
    unittest.main()
