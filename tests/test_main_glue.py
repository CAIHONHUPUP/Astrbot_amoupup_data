# -*- coding: utf-8 -*-
"""入口层装配胶水：main.py 的兼容垫片/配置包装/命令回退。

这些分支平时只被老面板或迁移期代码触发，最容易在重构后悄悄坏掉。
用 unittest.TestCase 写，保证 `python -m unittest discover` 与 pytest 都能跑到。
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

import main as entry  # noqa: E402
from main import AmoDataPlugin, _PluginStoreView  # noqa: E402
from utils.config import PluginSettings  # noqa: E402


class FakeDB:
    def __init__(self):
        self.conn = object()
        self.lock = object()
        self._conn = object()
        self.closed = False
        self.path = "x.db"

    def close(self):
        self.closed = True

    def bot_ids(self):
        return ["bot-a"]


class FakeEngine:
    def __init__(self):
        self.flag = 0

    def ping(self):
        return "pong"


class FakeEvent:
    def __init__(self, text=""):
        self.message_str = text

    def plain_result(self, text):
        return text


def _plugin(**kwargs):
    plugin = object.__new__(AmoDataPlugin)
    plugin.config = kwargs.pop("config", {})
    plugin.settings = PluginSettings(plugin.config)
    plugin.db = kwargs.pop("db", FakeDB())
    plugin.memory = kwargs.pop("memory", None)
    for name in ("retrieval", "compression", "profiles", "persons", "messages",
                 "logs", "cache", "backup", "maintenance"):
        setattr(plugin, name, None)
    return plugin


async def _collect(plugin, text):
    return [item async for item in plugin.forget_cmd(FakeEvent(text))]


class CommandFallbackTests(unittest.TestCase):
    def test_command_fallback_without_filter(self):
        def plain(func):
            return func

        old = entry._filter
        entry._filter = None
        try:
            deco = entry._command("x")
            self.assertIs(deco(plain), plain)
        finally:
            entry._filter = old


class StoreViewTests(unittest.TestCase):
    def test_properties(self):
        plugin = _plugin()
        view = _PluginStoreView(plugin)
        self.assertIs(view.conn, plugin.db.conn)
        self.assertIs(view.lock, plugin.db.lock)
        self.assertIs(view._conn, plugin.db._conn)
        self.assertIsNone(view._loop_task)
        self.assertEqual(view.setting("missing", 7), 7)
        view.close()
        self.assertTrue(plugin.db.closed)

    def test_delegates_to_engine_and_db(self):
        plugin = _plugin()
        engine = FakeEngine()
        plugin.cache = engine
        view = _PluginStoreView(plugin)
        self.assertEqual(view.ping(), "pong")
        view.flag = 5
        self.assertEqual(engine.flag, 5)
        self.assertIs(view.conn, plugin.db.conn)

    def test_unknown_attr_raises(self):
        view = _PluginStoreView(_plugin())
        with self.assertRaises(AttributeError):
            view.no_such_thing


class BotIdTests(unittest.TestCase):
    def test_resolution(self):
        plugin = _plugin(config={"bot_id": ""})
        self.assertEqual(plugin._bot_id({"bot_id": "  x  "}), "x")
        self.assertEqual(plugin._bot_id({}, write=False), "")
        plugin.config = {"bot_id": "conf-bot"}
        self.assertEqual(plugin._bot_id({}), "conf-bot")
        plugin.config = {"bot_id": ""}
        self.assertEqual(plugin._bot_id({}, write=True), "bot-a")

    def test_write_falls_back_on_db_error(self):
        plugin = _plugin()

        def boom():
            raise RuntimeError("db locked")

        plugin.db.bot_ids = boom
        self.assertEqual(plugin._bot_id({}, write=True), entry.DEFAULT_BOT_ID)


class ConfigWrapperTests(unittest.TestCase):
    def test_wrappers_and_properties(self):
        plugin = _plugin(config={"chat_days": 9, "chat_compress_after": 12, "log_days": 3})
        self.assertEqual(plugin._cfg_int("chat_days", 1), 9)
        self.assertEqual(plugin._cfg_float("chat_compress_after", 1.0), 12.0)
        self.assertIs(plugin._cfg_bool("no_such", True), True)
        self.assertTrue(plugin._store_settings())
        self.assertEqual(plugin.chat_days, 9)
        self.assertEqual(plugin.compress_after, 12)
        self.assertEqual(plugin.log_days, 3)


class ForgetCommandTests(unittest.TestCase):
    def test_requires_keyword(self):
        plugin = _plugin()
        out = asyncio.run(_collect(plugin, "/遗忘"))
        self.assertIn("用法", out[0])

    def test_success_and_failure(self):
        class Ok:
            def forget(self, bot, keyword):
                assert keyword == "猫"
                return 3

        plugin = _plugin(memory=Ok())
        plugin._bot_id = lambda *a, **k: ""
        self.assertIn("3 条", asyncio.run(_collect(plugin, "/遗忘 猫"))[0])

        class Boom:
            def forget(self, bot, keyword):
                raise RuntimeError("nope")

        plugin = _plugin(memory=Boom())
        plugin._bot_id = lambda *a, **k: ""
        self.assertIn("失败", asyncio.run(_collect(plugin, "/遗忘 猫"))[0])


class TerminateTests(unittest.TestCase):
    def test_terminate_is_safe_without_tasks(self):
        plugin = _plugin()

        class Maintenance:
            async def stop(self):
                return None

        plugin.maintenance = Maintenance()
        plugin._loop_task = None
        asyncio.run(plugin.terminate())
        self.assertTrue(plugin.db.closed)


if __name__ == "__main__":
    unittest.main()
