# -*- coding: utf-8 -*-
"""严格 mypy 棘轮逻辑（不跑 mypy，只测分类与基线文件）。"""
import importlib.util
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "mypy_strict.py")


def load_tool():
    spec = importlib.util.spec_from_file_location("mypy_strict_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StrictRatchetTests(unittest.TestCase):
    def test_classify(self):
        tool = load_tool()
        self.assertEqual(tool.classify(600, 564), "over")
        self.assertEqual(tool.classify(564, 564), "ok")
        self.assertEqual(tool.classify(500, 564), "lower")

    def test_parse_found_and_clean(self):
        tool = load_tool()
        self.assertEqual(tool.parse("Found 12 errors in 3 files (checked 200 source files)"),
                         (12, 3, 200))
        self.assertEqual(tool.parse("Success: no issues found in 220 source files"), (0, 0, 220))
        self.assertIsNone(tool.parse("boom"))

    def test_baseline_file_shape(self):
        with open(os.path.join(ROOT, "tools", "mypy_strict_baseline.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertGreaterEqual(int(data["errors"]), 0)
        self.assertIn("updated", data)


if __name__ == "__main__":
    unittest.main()
