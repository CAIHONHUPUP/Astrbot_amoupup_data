# -*- coding: utf-8 -*-
"""写入时实体标签的单测。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import entity_tag  # noqa: E402


class EntityTagTests(unittest.TestCase):
    def test_matches_known_names(self):
        self.assertEqual(
            entity_tag.match_names("小周今天换了工作，团子还在家等我", ["小周", "团子"]),
            ["小周", "团子"])

    def test_ignores_short_and_absent(self):
        self.assertEqual(entity_tag.match_names("今天天气不错", ["小", "阿明"]), [])

    def test_caps_entities(self):
        names = ["甲一", "乙二", "丙三", "丁四"]
        self.assertEqual(
            entity_tag.match_names("甲一乙二丙三丁四都来了", names),
            ["甲一", "乙二", "丙三"])

    def test_apply_adds_without_duplicates(self):
        tags = entity_tag.apply_entity_tags("小周来了", "#活动 event:7", ["小周"])
        self.assertEqual(tags, "#活动 event:7 entity:小周")
        again = entity_tag.apply_entity_tags("小周又来了", tags, ["小周"])
        self.assertEqual(again, tags, "同名实体不能重复挂")

    def test_empty_tags_stay_empty(self):
        self.assertEqual(entity_tag.apply_entity_tags("没有名字", "", ["小周"]), "")


if __name__ == "__main__":
    unittest.main()
