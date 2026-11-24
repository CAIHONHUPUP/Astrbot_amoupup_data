# -*- coding: utf-8 -*-
"""删除画像时同步清理能陪侧（_forget_in_amo）单测，从 repo 的 test_profile_delete_sync.py 迁到新布局。

旧文件打的是 `main.AmoDataPlugin`；新布局里 `_forget_in_amo` 在 `core/handlers/base.py` 的 BaseHandler。
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from _harness import Stack  # noqa: E402,F401  （确保 astrbot 桩已装）
from core.handlers.base import HandlerBaseMixin  # noqa: E402


class _FakeDB:
    def __init__(self):
        self.deleted = []

    def delete_profile(self, uid):
        self.deleted.append(uid)


class _FakeAmo:
    def __init__(self):
        self.db = _FakeDB()


class _Ctx:
    def __init__(self, amo):
        self.amo = amo

    def get_registered_star(self, name):
        return types.SimpleNamespace(star_cls=self.amo)


def _handler(ctx):
    obj = object.__new__(HandlerBaseMixin)
    obj.context = ctx
    return obj


class ForgetInAmoTests(unittest.TestCase):
    def test_deletes_local_profile(self):
        amo = _FakeAmo()
        plugin = _handler(_Ctx(amo))
        self.assertTrue(plugin._forget_in_amo("u1"))
        self.assertEqual(amo.db.deleted, ["u1"])

    def test_missing_plugin_is_safe(self):
        plugin = _handler(_Ctx(None))
        self.assertFalse(plugin._forget_in_amo("u1"))

    def test_missing_db_is_safe(self):
        amo = types.SimpleNamespace()
        plugin = _handler(_Ctx(amo))
        self.assertFalse(plugin._forget_in_amo("u1"))

    def test_broken_context_is_safe(self):
        plugin = _handler(object())
        self.assertFalse(plugin._forget_in_amo("u1"))


class HandlerWiringTests(unittest.TestCase):
    def test_delete_handler_calls_forget(self):
        path = os.path.join(_ROOT, "core", "handlers", "profiles.py")
        src = open(path, encoding="utf-8").read()
        body = src[src.index("async def api_profile_delete"):src.index("async def api_persons")]
        self.assertIn("_forget_in_amo(uid)", body)


if __name__ == "__main__":
    unittest.main()
