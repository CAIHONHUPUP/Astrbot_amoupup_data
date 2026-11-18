# -*- coding: utf-8 -*-
"""查询改写（剥壳 / 去尾巴 / 拆词变体）的单测。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import query_rewrite  # noqa: E402


class VariantTests(unittest.TestCase):
    def test_shell_is_stripped(self):
        out = query_rewrite.variants("你还记得我上次说的那只猫吗")
        self.assertEqual(out[0], "你还记得我上次说的那只猫吗", "原句永远排第一")
        self.assertIn("我上次说的那只猫", out)

    def test_plain_question_gets_core_variant(self):
        out = query_rewrite.variants("团子最近怎么样呢")
        self.assertEqual(out[0], "团子最近怎么样呢")
        self.assertIn("团子最近怎么样", out)

    def test_short_and_empty(self):
        self.assertEqual(query_rewrite.variants(""), [])
        self.assertEqual(query_rewrite.variants("猫"), ["猫"])

    def test_terms_variant_is_space_joined_bigrams(self):
        out = query_rewrite.variants("你还记得团子那只橘猫吗")
        joined = [item for item in out if " " in item]
        self.assertTrue(joined, "应该有一个拆词变体给 token OR 用")
        self.assertIn("团子", joined[0])

    def test_max_variants_and_dedup(self):
        out = query_rewrite.variants("你还记得团子吗", 2)
        self.assertLessEqual(len(out), 2)
        self.assertEqual(len(out), len(set(out)), "变体不能重复")
        self.assertEqual(query_rewrite.variants("你还记得团子吗", 1), ["你还记得团子吗"])

    def test_head_filler_removed_only_at_start(self):
        self.assertIn("那只猫", query_rewrite.variants("那个那只猫呢"))
        self.assertIn("猫", query_rewrite.variants("上次说的猫呢"))
        interior = query_rewrite.variants("那只上次说的猫呢")
        self.assertIn("那只上次说的猫", interior, "中间出现的同词不能被删")


if __name__ == "__main__":
    unittest.main()
