# -*- coding: utf-8 -*-
"""时间解析补充单测（从 repo 的 test_parse_extra.py 迁到新布局）。

未迁 ImportFailureTests：它打的是 `store.import_snapshot`，新布局换成
BackupEngine/门面导入，需要单独设计（和 test_memory_api 的 SnapshotTests 一起）。
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from utils import clock  # noqa: E402
from utils import time_query as tq  # noqa: E402

NOW = datetime(2026, 11, 12, 15, 30)  # 周四


class ParseSpanTests(unittest.TestCase):
    def parse(self, text):
        return tq.parse_time_span(text, now=NOW)

    def test_recent_units(self):
        self.assertEqual(self.parse("最近3小时")["granularity"], "hour")
        self.assertEqual(self.parse("最近30分钟")["granularity"], "hour")
        self.assertEqual(self.parse("最近2天")["granularity"], "day")

    def test_weekday_future_rolls_back(self):
        span = self.parse("周日")
        self.assertEqual(span["day"], 8)  # 11-15 是未来，回退到 11-08

    def test_weekday_prefixes(self):
        self.assertEqual(self.parse("上周三")["day"], 4)
        self.assertEqual(self.parse("上上周三")["day"], 28)

    def test_year_tokens(self):
        span = self.parse("去年")
        self.assertEqual(span["granularity"], "year")
        self.assertEqual(clock.from_timestamp(span["start"]).year, 2025)
        self.assertEqual(clock.from_timestamp(self.parse("前年")["start"]).year, 2024)
        self.assertEqual(clock.from_timestamp(self.parse("今年")["start"]).year, 2026)

    def test_month_and_day(self):
        span = self.parse("11月")
        self.assertEqual(span["granularity"], "month")
        self.assertEqual(clock.from_timestamp(span["start"]).month, 11)
        span = self.parse("11月6号")
        self.assertEqual(span["granularity"], "day")
        self.assertEqual(span["day"], 6)
        self.assertEqual(self.parse("6号")["day"], 6)

    def test_month_aliases(self):
        self.assertEqual(clock.from_timestamp(self.parse("上个月")["start"]).month, 10)
        self.assertEqual(clock.from_timestamp(self.parse("这个月")["start"]).month, 11)

    def test_hour_periods(self):
        self.assertEqual(self.parse("中午12点")["hour"], 12)
        self.assertEqual(self.parse("晚上12点")["hour"], 0)
        self.assertEqual(self.parse("深夜1点")["hour"], 1)
        self.assertEqual(self.parse("深夜11点")["hour"], 23)
        self.assertEqual(self.parse("下午3点")["hour"], 15)
        self.assertEqual(self.parse("凌晨12点")["hour"], 0)

    def test_period_only_when_current(self):
        span = self.parse("下午")
        self.assertEqual(span["granularity"], "hour")
        self.assertIsNone(self.parse("凌晨"))

    def test_invalid_date_returns_none(self):
        self.assertIsNone(self.parse("2月30号"))

    def test_no_time_returns_none(self):
        self.assertIsNone(self.parse("随便聊聊"))
        self.assertIsNone(self.parse(""))

    def test_describe(self):
        self.assertEqual(tq.describe_span(None), "没提时间")
        self.assertIn("年", tq.describe_span(self.parse("去年")))
        self.assertIn("月", tq.describe_span(self.parse("11月")))
        self.assertIn(":", tq.describe_span(self.parse("11月6号下午3点")))
        self.assertIn("~", tq.describe_span(self.parse("最近3天")))
        self.assertIn("-", tq.describe_span(self.parse("11月6号")))

    def test_cn_value(self):
        self.assertEqual(tq._cn_value("六"), 6)
        self.assertEqual(tq._cn_value("十一"), 11)
        self.assertEqual(tq._cn_value("二十三"), 23)
        self.assertEqual(tq._cn_value(""), -1)
        self.assertEqual(tq._cn_value("甲"), -1)
        self.assertEqual(tq._cn_value("十十"), -1)
        self.assertEqual(tq._cn_value("十甲"), -1)

    def test_normalize_numbers(self):
        self.assertEqual(tq.normalize_numbers("六号"), "6号")
        self.assertEqual(tq.normalize_numbers("没数字"), "没数字")


class ClockExtraTests(unittest.TestCase):
    def test_zone_fallback_without_zoneinfo(self):
        original = clock.ZoneInfo
        clock.ZoneInfo = None
        try:
            self.assertIsNotNone(clock.get_zone("Asia/Shanghai"))
            self.assertIsNone(clock.get_zone("Not/AZone"))
        finally:
            clock.ZoneInfo = original

    def test_now_and_offset_with_unknown_zone(self):
        self.assertIsNotNone(clock.now("Not/AZone"))
        self.assertIsInstance(clock.utc_offset_minutes("Not/AZone"), int)
        self.assertIsNotNone(clock.from_timestamp(0, "Not/AZone"))

    def test_etc_timezone_read(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="Asia/Tokyo\n")):
            self.assertEqual(clock._read_etc_timezone(), "Asia/Tokyo")

    def test_restore_with_previous_tz(self):
        clock._applied.update({"ok": True, "name": "Asia/Shanghai", "prev": "UTC"})
        original = clock._time
        clock._time = mock.Mock()
        try:
            clock.restore()
        finally:
            clock._time = original
        self.assertEqual(os.environ.get("TZ"), "UTC")
        os.environ.pop("TZ", None)

    def test_applied_false_is_safe(self):
        clock.restore()
        self.assertFalse(clock.applied())


if __name__ == "__main__":
    unittest.main()
