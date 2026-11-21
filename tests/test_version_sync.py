# -*- coding: utf-8 -*-
"""版本号单源：面板 /status 报的是 utils/config.py 的值，必须和 main.py 一致。

2026-11-15 线上：main.py / metadata 早就 3.2.4，utils/config.py 还停在 3.2.1，
面板一直显示旧版本；check_version.py 当时只查 main.py，漏了这里。
"""
from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

import main as main_module  # noqa: E402
from utils import config as config_module  # noqa: E402


class VersionSyncTests(unittest.TestCase):
    def test_config_matches_main(self):
        self.assertEqual(config_module.PLUGIN_VERSION, main_module.PLUGIN_VERSION)

    def test_metadata_matches_code(self):
        with open(os.path.join(ROOT, "metadata.yaml"), encoding="utf-8") as fh:
            text = fh.read()
        match = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", text, re.M)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), main_module.PLUGIN_VERSION)


if __name__ == "__main__":
    unittest.main()
