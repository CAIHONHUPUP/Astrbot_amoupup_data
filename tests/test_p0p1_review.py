# -*- coding: utf-8 -*-
"""2026-11-16 P0/P1 审查回归：写记忆撞唯一索引、面板删记忆空转、召回注入去重失效、
坏备份静默"恢复成功"、周整理阻塞事件循环。

全部离线：不联网、不依赖 embedding 服务、不碰 plugin_data。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

import main as main_module  # noqa: E402
from main import AmoDataPlugin, MemoryAPI  # noqa: E402
from services import retrieval_engine as retrieval_module  # noqa: E402
from support.store_fixture import DataStore  # noqa: E402

BOT = "default"
UID = "302578963"


def _new_store(tmp: str) -> DataStore:
    return DataStore(
        os.path.join(tmp, "amo_data.db"),
        {"embedding_enabled": False, "embedding_backfill": False},
    )


class _Ctx:
    """最小 Context：只要 register_web_api / get_registered_star 不炸就行。"""

    registered_web_apis: list = []

    def register_web_api(self, *args, **kwargs):
        return None

    def get_config(self, umo=None):
        return {}


class _Req:
    def __init__(self, payload):
        self.method = "POST"
        self.headers = {}
        self._payload = payload

    async def json(self, default=None):
        return self._payload


class _PluginCase(unittest.TestCase):
    """默认配置（config 里没有 bot_id，等于 "default"）的插件实例，DB 落临时目录。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="amo-data-review-")
        self._saved = {}
        for name in ("astrbot.core.utils", "astrbot.core.utils.astrbot_path"):
            self._saved[name] = sys.modules.get(name)
        utils = types.ModuleType("astrbot.core.utils")
        utils.__path__ = []
        path_module = types.ModuleType("astrbot.core.utils.astrbot_path")
        path_module.get_astrbot_data_path = lambda: self.tmp
        sys.modules["astrbot.core.utils"] = utils
        sys.modules["astrbot.core.utils.astrbot_path"] = path_module
        self._saved_request = main_module.web_request
        main_module.web_request = None
        self.plugin = AmoDataPlugin(_Ctx(), {})

    def tearDown(self) -> None:
        try:
            self.plugin.store.close()
        except Exception:
            pass
        main_module.web_request = self._saved_request
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def run_api(self, handler, payload):
        main_module.web_request = _Req(payload)
        try:
            return asyncio.run(handler())
        finally:
            main_module.web_request = self._saved_request


class MemoryReactivationTests(unittest.TestCase):
    """旧行归档/取代后，再写同一件事不能撞 idx_mem_idem 把整条记忆丢掉。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="amo-data-review-")
        self.store = _new_store(self.tmp)

    def tearDown(self) -> None:
        self.store.close()

    def _status(self, memory_id: int) -> str:
        row = self.store.conn.execute(
            "SELECT status FROM memories WHERE id=?", (int(memory_id),)
        ).fetchone()
        return str(row["status"]) if row else ""

    def _count(self) -> int:
        return int(self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM memories").fetchone()["c"])

    def test_fact_after_archive_is_written_again(self) -> None:
        first = self.store.remember(BOT, UID, "主人爱吃螺蛳粉", kind="fact")
        self.assertTrue(first["ok"])
        self.store.memory.nightly_archive([first["memory_id"]])
        self.assertEqual(self._status(first["memory_id"]), "archived")

        again = self.store.remember(BOT, UID, "主人爱吃螺蛳粉", kind="fact")
        self.assertTrue(again["ok"], "归档后再写同一件事不能撞唯一索引丢写")
        self.assertEqual(again["memory_id"], first["memory_id"])
        self.assertEqual(self._status(first["memory_id"]), "active")
        self.assertEqual(self._count(), 1)

    def test_fact_after_supersede_is_written_again(self) -> None:
        beijing = self.store.remember(BOT, UID, "主人住在北京")
        moved = self.store.remember(BOT, UID, "主人住在上海")
        self.assertEqual(moved["superseded"], [beijing["memory_id"]])

        back = self.store.remember(BOT, UID, "主人住在北京")
        self.assertTrue(back["ok"], "搬回来再说一次不能丢写")
        self.assertEqual(back["memory_id"], beijing["memory_id"])
        self.assertEqual(self._status(beijing["memory_id"]), "active")

    def test_memory_api_reports_success(self) -> None:
        api = MemoryAPI(self.store)
        first = asyncio.run(api.remember(BOT, UID, "主人爱吃螺蛳粉"))
        self.assertTrue(first["ok"])
        self.store.memory.nightly_archive([first["memory_id"]])
        again = asyncio.run(api.remember(BOT, UID, "主人爱吃螺蛳粉"))
        self.assertTrue(again["ok"])
        self.assertNotIn("UNIQUE", str(again.get("reason") or ""))


class PanelMemoryDeleteTests(_PluginCase):
    """面板「删」只传 {id}；默认 bot_id 下必须真的删掉，不能 code:0 空转。"""

    def test_delete_with_default_bot_config(self) -> None:
        self.plugin.store.remember("default", UID, "她记得我养了一只叫团子的猫")
        items = self.plugin.store.list_memories("default", UID, 10)
        self.assertEqual(len(items), 1)

        out = self.run_api(self.plugin.api_memory_delete, {"id": items[0]["id"]})
        self.assertEqual(out["code"], 0)
        self.assertTrue(out["data"]["deleted"], "面板只传 id 时不能静默删 0 条")
        self.assertEqual(self.plugin.store.list_memories("default", UID, 10), [])

    def test_delete_resolves_owner_bot_in_multi_bot_db(self) -> None:
        self.plugin.store.remember("botA", UID, "A 的记忆")
        self.plugin.store.remember("botB", UID, "B 的记忆")
        target = self.plugin.store.list_memories("botB", UID, 10)
        self.assertEqual(len(target), 1)

        out = self.run_api(self.plugin.api_memory_delete, {"id": target[0]["id"]})
        self.assertTrue(out["data"]["deleted"], "多 bot 库要按 id 反查 bot_id，不能删漏")
        self.assertEqual(self.plugin.store.list_memories("botB", UID, 10), [])
        self.assertEqual(len(self.plugin.store.list_memories("botA", UID, 10)), 1)


class RecallInjectionDedupeTests(unittest.TestCase):
    """AstrBot 是 data.plugins.<插件> 包模式，没有顶层 services 模块。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="amo-data-review-")
        self.store = _new_store(self.tmp)
        self.store.remember(BOT, UID, "主人爱吃螺蛳粉", kind="fact")
        self.store.remember(BOT, UID, "今天天气不错", kind="fact")
        self._saved_fact_key = retrieval_module.fact_dedupe.fact_key
        retrieval_module.fact_dedupe.fact_key = lambda text: "same-fact"
        self._saved_services = sys.modules.get("services")
        sys.modules["services"] = None  # import services -> ImportError

    def tearDown(self) -> None:
        retrieval_module.fact_dedupe.fact_key = self._saved_fact_key
        if self._saved_services is None:
            sys.modules.pop("services", None)
        else:
            sys.modules["services"] = self._saved_services
        self.store.close()

    def test_dedupe_survives_missing_services_module(self) -> None:
        rows = self.store.recall(BOT, UID, "")
        self.assertEqual(
            len(rows), 1,
            "同一事实只该注入一条；两条说明注入去重因 ImportError 静默失效了",
        )


class CorruptBackupRestoreTests(unittest.TestCase):
    """坏备份 / 不是能记的库：必须报错，不能装成「恢复成功 0 条」。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="amo-data-review-")
        self.store = _new_store(self.tmp)
        self.backup_dir = self.store.backup.backup_dir()
        os.makedirs(self.backup_dir, exist_ok=True)

    def tearDown(self) -> None:
        self.store.close()

    def _write(self, name: str, payload) -> str:
        path = os.path.join(self.backup_dir, name)
        if isinstance(payload, bytes):
            with open(path, "wb") as fh:
                fh.write(payload)
        else:
            conn = sqlite3.connect(path)
            conn.execute(payload)
            conn.commit()
            conn.close()
        return name

    def test_garbage_file_is_an_error(self) -> None:
        name = self._write("amo_data-20260101-000000.db", b"not a sqlite database")
        self.assertEqual(self.store.backup.restore_backup(name), {"error": 1})

    def test_foreign_sqlite_is_an_error(self) -> None:
        name = self._write("amo_data-20260102-000000.db",
                           "CREATE TABLE unrelated(x INTEGER)")
        self.assertEqual(self.store.backup.restore_backup(name), {"error": 1})


class MaintenanceThreadingTests(_PluginCase):
    """周整理 / 面板整理都是同步重活，必须在工作线程跑。"""

    def test_weekly_cleanup_does_not_block_event_loop(self) -> None:
        slept = []

        def slow_compress(bot_id, apply=True):
            slept.append(bot_id)
            time.sleep(0.25)
            return {"merged_summaries": 0}

        self.plugin.compression.compress_memories = slow_compress
        self.plugin.compression.archive_stale = lambda *args, **kwargs: {"archived": 0}

        async def scenario() -> int:
            ticks = 0

            async def ticker():
                nonlocal ticks
                while True:
                    await asyncio.sleep(0.01)
                    ticks += 1

            task = asyncio.create_task(ticker())
            try:
                await self.plugin.maintenance._maybe_weekly_cleanup("default")
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            return ticks

        ticks = asyncio.run(scenario())
        self.assertEqual(slept, ["default"])
        self.assertGreater(ticks, 5, "压缩放线程后事件循环应该还能跑心跳")

    def test_memory_api_compress_runs_off_main_thread(self) -> None:
        seen = {}
        api = MemoryAPI(self.plugin)

        def fake_compress(bot_id, apply=True):
            seen["thread"] = threading.current_thread().name
            return {"merged_summaries": 0}

        self.plugin.compression.compress_memories = fake_compress
        out = asyncio.run(api.compress_memories("default"))
        self.assertEqual(out, {"merged_summaries": 0})
        self.assertNotEqual(seen["thread"], threading.main_thread().name)


if __name__ == "__main__":
    unittest.main()
