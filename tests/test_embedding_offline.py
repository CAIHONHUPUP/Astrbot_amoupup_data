# -*- coding: utf-8 -*-
"""没装 embedding 服务的新用户，不该被语义召回拖慢，也不该一脸懵。

以前 `embedding_enabled` 默认开、地址默认指本机 Ollama：没装的人每条消息最多
多等 3 秒，语义召回一直是空的，日志里什么都没有。
现在：默认关；真开了但连不上，只提醒一次「已退回关键词」。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import services.memory_engine as store_memory  # noqa: E402
from support.store_fixture import DataStore  # noqa: E402


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(str(message))

    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class EmbeddingOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"))
        self.saved_logger = store_memory.logger
        self.logger = FakeLogger()
        store_memory.logger = self.logger
        self.settings = {
            "embedding_enabled": True,
            "embedding_base": "http://127.0.0.1:9",   # 本机一定没人监听
            "embedding_model": "bge-m3",
            "embedding_timeout": 0.2,
        }
        self.store.setting = lambda key, default=None: self.settings.get(key, default)

    def tearDown(self):
        store_memory.logger = self.saved_logger
        self.store.close()

    def test_schema_ships_with_embedding_off(self):
        path = os.path.join(ROOT, "_conf_schema.json")
        with open(path, encoding="utf-8-sig") as fh:
            schema = json.load(fh)
        self.assertIs(schema["embedding_enabled"]["default"], False)

    def test_disabled_does_not_call_or_warn(self):
        self.settings["embedding_enabled"] = False
        self.assertEqual(self.store._embed("他喜欢吃番茄牛腩"), [])
        self.assertEqual(self.logger.warnings, [])

    def _related_warnings(self):
        """只数这个 store 的告警（bge-m3 / 127.0.0.1:9）。

        logger 是模块级全局：别的测试留下的 embedding worker 线程还在跑时，
        它也会往同一个 logger 写告警。不过滤的话「只提醒一次」会被并发告警
        干扰成全量跑必红、单跑必过。
        """
        return [w for w in self.logger.warnings
                if "退回关键词" in w and "bge-m3" in w and "127.0.0.1:9" in w]

    def test_unreachable_service_warns_only_once(self):
        self.logger.warnings.clear()
        self.assertEqual(self.store._embed("他喜欢吃番茄牛腩"), [])
        related = self._related_warnings()
        self.assertEqual(len(related), 1, self.logger.warnings)
        self.assertIn("退回关键词", related[0])
        # 60 秒退避内的第二次调用：不再尝试，也不再提醒
        self.assertEqual(self.store._embed("他又说了一句"), [])
        self.assertEqual(len(self._related_warnings()), 1)

    def test_enabled_state_is_reported_in_stats(self):
        stats = self.store.embedding_stats()
        self.assertTrue(stats["enabled"])
        self.settings["embedding_enabled"] = False
        self.assertFalse(self.store.embedding_stats()["enabled"])


if __name__ == "__main__":
    unittest.main()
