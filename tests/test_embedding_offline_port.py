# -*- coding: utf-8 -*-
"""embedding 服务不可用时的降级单测（从 repo 的 test_embedding_offline.py 迁到新布局）。

新布局里 embedding 在 MemoryEngine（`_embed` / `embedding_stats`），日志器是 services.memory_engine.logger。
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import Stack  # noqa: E402
import services.memory_engine as memory_engine  # noqa: E402


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(str(message) % args if args else str(message))

    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class EmbeddingOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-embed-")
        self.saved_logger = memory_engine.logger
        self.logger = FakeLogger()
        memory_engine.logger = self.logger
        # 本机一定没人监听 127.0.0.1:9 → 必定失败且不会真的等太久
        self.stack = Stack(self.tmp, "amo_data.db", settings={
            "embedding_enabled": True,
            "embedding_base": "http://127.0.0.1:9",
            "embedding_model": "bge-m3",
            "embedding_timeout": 0.2,
            "embedding_backfill": False,
        })

    def tearDown(self):
        memory_engine.logger = self.saved_logger
        self.stack.close()

    def test_schema_ships_with_embedding_off(self):
        with open(os.path.join(ROOT, "_conf_schema.json"), encoding="utf-8-sig") as fh:
            schema = json.load(fh)
        self.assertIs(schema["embedding_enabled"]["default"], False)

    def test_disabled_does_not_call_or_warn(self):
        self.stack.memory.settings["embedding_enabled"] = False
        self.assertEqual(self.stack.memory._embed("他喜欢吃番茄牛腩"), [])
        self.assertEqual(self.logger.warnings, [])

    def test_unreachable_service_warns_only_once(self):
        self.assertEqual(self.stack.memory._embed("他喜欢吃番茄牛腩"), [])
        self.assertEqual(len(self.logger.warnings), 1)
        self.assertIn("退回关键词", self.logger.warnings[0], self.logger.warnings)
        # 60 秒退避内的第二次调用：不再尝试，也不再提醒
        self.assertEqual(self.stack.memory._embed("他又说了一句"), [])
        self.assertEqual(len(self.logger.warnings), 1)

    def test_enabled_state_is_reported_in_stats(self):
        self.assertTrue(self.stack.memory.embedding_stats()["enabled"])
        self.stack.memory.settings["embedding_enabled"] = False
        self.assertFalse(self.stack.memory.embedding_stats()["enabled"])


if __name__ == "__main__":
    unittest.main()
