# -*- coding: utf-8 -*-
"""clock.freeze / unfreeze 行为契约（弱点2：时间耦合收口）。

给定冻结点 X → 必须：插件内 now/now_ts/today/today_str 全部等于 X；
解除冻结 → 必须回到真实墙钟；全量测试的 autouse fixture 必须默认钉在 2026-11-18 15:00。
反向验证见 work/reverse_checks_specs.py（摘掉 _frozen 分支 → 本文件必红）。
"""
import os
import sys
import time
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from utils import clock  # noqa: E402

FROZEN = datetime(2026, 11, 18, 15, 0, 0)


class FreezeContractTests(unittest.TestCase):
    def tearDown(self):
        clock.unfreeze()

    def test_autouse_fixture_freezes_default_time(self):
        """conftest 的 autouse 必须把全量测试钉在 2026-11-18 15:00（跨天不再变红）。"""
        self.assertEqual(clock.now(), FROZEN)
        self.assertEqual(clock.today_str(), "2026-11-18")

    def test_freeze_datetime_and_unfreeze(self):
        clock.freeze(FROZEN)
        self.assertEqual(clock.now(), FROZEN)
        self.assertEqual(clock.today_str(), "2026-11-18")
        self.assertAlmostEqual(clock.now_ts(), FROZEN.timestamp(), places=3)
        self.assertAlmostEqual(clock.frozen(), FROZEN.timestamp(), places=3)
        clock.unfreeze()
        self.assertIsNone(clock.frozen())
        self.assertLess(abs(time.time() - clock.now_ts()), 5)
        self.assertLess(abs((clock.now() - datetime.fromtimestamp(time.time())).total_seconds()), 5)

    def test_freeze_epoch_seconds(self):
        clock.freeze(FROZEN.timestamp())
        self.assertEqual(clock.now(), FROZEN)

    def test_freeze_overrides_previous_point(self):
        clock.freeze(FROZEN)
        clock.freeze(datetime(2026, 11, 20, 9, 30))
        self.assertEqual(clock.today_str(), "2026-11-20")
        self.assertEqual(clock.now().hour, 9)

    def test_freeze_does_not_change_configured_timezone(self):
        before = clock.current_name()
        clock.freeze(FROZEN)
        self.assertEqual(clock.current_name(), before)
        self.assertEqual(clock.now().hour, 15)

    def test_freeze_keeps_zone_conversion(self):
        clock.freeze(FROZEN)
        self.assertEqual(clock.today(), FROZEN.date())
        self.assertEqual(clock.today_str("Asia/Shanghai"), "2026-11-18")


if __name__ == "__main__":
    unittest.main()
