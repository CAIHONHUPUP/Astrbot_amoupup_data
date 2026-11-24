# -*- coding: utf-8 -*-
"""时区单测（从 repo 的 test_clock.py 迁到新布局：clock → utils.clock）。

Windows 没有 tzset，进程级时区改不了 → 这些用例在 Windows 本地跳过，
在 GitHub Actions（Ubuntu）上会真正执行。
"""
import os
import sys
import time
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from utils import clock  # noqa: E402


@unittest.skipUnless(hasattr(time, "tzset"), "Windows 没有 tzset，进程级时区改不了")
class ClockTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"   # 模拟线上 AstrBot 容器
        time.tzset()
        clock.configure("UTC")

    def tearDown(self):
        clock.restore()
        if self._prev is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._prev
        time.tzset()

    def test_configure_moves_process_clock_by_eight_hours(self):
        before = datetime.now()
        self.assertEqual(clock.configure("Asia/Shanghai"), "Asia/Shanghai")
        after = datetime.now()
        self.assertAlmostEqual((after - before).total_seconds(), 8 * 3600, delta=5)

    def test_now_agrees_with_process_clock(self):
        clock.configure("Asia/Shanghai")
        self.assertLess(abs((clock.now() - datetime.now()).total_seconds()), 2)

    def test_timestamp_conversion_is_aligned(self):
        """关键：naive 时间的 .timestamp() 必须按目标时区解释（SQLite/时间检索都靠它）。"""
        clock.configure("Asia/Shanghai")
        local = datetime(2026, 11, 10, 10, 0, 0)
        expected = datetime(2026, 11, 10, 2, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(int(local.timestamp()), int(expected), "北京 10 点应等于 UTC 02 点")

    def test_sql_offset_modifier(self):
        clock.configure("Asia/Shanghai")
        self.assertEqual(clock.utc_offset_minutes(), 480)
        self.assertEqual(clock.sql_offset_modifier(), "+480 minutes")

    def test_from_timestamp_uses_target_zone(self):
        clock.configure("Asia/Shanghai")
        ts = datetime(2026, 11, 10, 2, 0, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(clock.from_timestamp(ts).hour, 10)


if __name__ == "__main__":
    unittest.main()
