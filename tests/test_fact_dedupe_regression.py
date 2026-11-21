# -*- coding: utf-8 -*-
"""否句不能把无关记忆成片归档（2026-11-21 P0）。"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.fact_dedupe import should_expire  # noqa: E402


class FactDedupeRegressionTests(unittest.TestCase):
    def test_negative_preference_does_not_expire_unrelated_fact(self):
        self.assertFalse(should_expire("我今天买了草莓蛋糕", "我不喜欢草莓"))

    def test_same_fact_key_still_expires(self):
        self.assertTrue(should_expire("我喜欢吃辣", "我不喜欢吃辣"))


if __name__ == "__main__":
    unittest.main()
