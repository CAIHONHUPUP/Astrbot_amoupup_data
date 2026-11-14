# -*- coding: utf-8 -*-
"""零散但真实存在的分支：请求代理、缓存坏数据、空日志、空 uid 画像。"""
from __future__ import annotations

import sqlite3
import sys
import threading
import types
import unittest
from unittest import mock

from core.web_request import _RequestProxy
from services.cache_engine import CacheEngine
from services.log_engine import LogEngine
from services.profile_engine import ProfileEngine


class FakeDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE cache(bot_id TEXT, key TEXT, value TEXT, "
            "updated_at INTEGER, expires_at INTEGER, PRIMARY KEY(bot_id, key))"
        )
        self.lock = threading.Lock()


def _isolated_modules():
    """别的测试会先把 main 导进来并挂上 web_request；这里换成空模块，测代理自身分支。"""
    return mock.patch.dict(sys.modules, {
        "main": types.SimpleNamespace(),
        "Astrbot_amoupup_data.main": types.SimpleNamespace(),
    })


class RequestProxyTests(unittest.TestCase):
    def test_missing_target(self):
        with _isolated_modules():
            proxy = _RequestProxy(None)
            self.assertFalse(proxy)
            with self.assertRaises(AttributeError):
                proxy.anything

    def test_delegates_to_fallback(self):
        class Raw:
            flag = 1

        with _isolated_modules():
            proxy = _RequestProxy(Raw())
            self.assertTrue(proxy)
            self.assertEqual(proxy.flag, 1)


class CacheBranchTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.cache = CacheEngine(self.db)

    def test_corrupt_json_returns_none(self):
        self.db.conn.execute(
            "INSERT INTO cache(bot_id, key, value, updated_at, expires_at) VALUES(?,?,?,?,?)",
            ("b", "k", "{not-json", 0, 0),
        )
        self.db.conn.commit()
        self.assertIsNone(self.cache.cache_get("b", "k"))

    def test_unserializable_value_is_rejected(self):
        self.assertFalse(self.cache.cache_set("b", "k", object()))

    def test_roundtrip(self):
        self.assertTrue(self.cache.cache_set("b", "k", {"a": 1}))
        self.assertEqual(self.cache.cache_get("b", "k"), {"a": 1})


class LogBranchTests(unittest.TestCase):
    def test_setting_falls_back_to_dict(self):
        engine = LogEngine(FakeDB())
        self.assertEqual(engine.setting("missing", 5), 5)

    def test_empty_log_is_ignored(self):
        self.assertEqual(LogEngine(FakeDB()).add_log("b", "info", "t", "   "), 0)


class ProfileBranchTests(unittest.TestCase):
    def test_setting_and_empty_uid(self):
        engine = ProfileEngine(FakeDB())
        self.assertEqual(engine.setting("missing", 7), 7)
        self.assertEqual(engine.find_profile(""), {})


if __name__ == "__main__":
    unittest.main()
