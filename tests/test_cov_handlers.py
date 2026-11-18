# -*- coding: utf-8 -*-
"""覆盖率补齐：core/handlers 的组合入口兜底与 34 条接口里没走到的分支。

接口逻辑直接用 mixin + 桩宿主调用，不启插件（不碰真库）：
* memory：保存失败、平台回填成功/异常、生命周期预演/执行、手动整理；
* messages：群列表、群改名两条返回、limit 非法值回落 100；
* relations：画像里显式关系覆盖已有关联、没有关联时补一条、名字回填；
* base：陪伴插件查找失败、删画像同步失败、请求体 json 解析失败回落；
* handlers/__init__：顶层导入（相对导入失败）的回退分支。
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from core.handlers import base as base_module
from core.handlers.base import HandlerBaseMixin
from core.handlers.memory import MemoryHandlersMixin
from core.handlers.messages import MessageHandlersMixin
from core.handlers.relations import RelationHandlersMixin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Host(HandlerBaseMixin, MemoryHandlersMixin, MessageHandlersMixin, RelationHandlersMixin):
    """最小宿主：提供 _body/_bot_id 和被测接口用到的组件。"""

    def __init__(self, payload=None, **components):
        self.payload = payload or {}
        self.bot_id = "b"
        self.context = None
        for name, value in components.items():
            setattr(self, name, value)

    async def _body(self):
        return self.payload

    def _bot_id(self, payload, write=False):
        return self.bot_id


def _async(return_value):
    async def fn(*args, **kwargs):
        return return_value
    return fn


class MemoryHandlerTests(unittest.TestCase):
    def test_save_failure_reports_reason(self) -> None:
        memory = SimpleNamespace(remember=lambda *a, **k: {"ok": False, "reason": "重复"})
        host = _Host({"uid": "u", "content": "内容"}, memory=memory)
        out = asyncio.run(host.api_memory_save())
        self.assertEqual(out["code"], 1)
        self.assertEqual(out["msg"], "已记住")
        self.assertEqual(out["data"]["reason"], "重复")

    def test_backfill_platform_success_and_error(self) -> None:
        calls = []

        def backfill(bot, limit, scan, apply):
            calls.append((bot, limit, scan, apply))
            return {"scanned": 10, "filled": 3}

        host = _Host({"apply": "false"},
                     memory=SimpleNamespace(backfill_memory_platform=backfill))
        out = asyncio.run(host.api_memory_backfill_platform())
        self.assertEqual(out["code"], 0)
        self.assertFalse(out["applied"])
        self.assertEqual(calls, [("b", 600, 20000, False)])

        def boom(*args, **kwargs):
            raise RuntimeError("回填炸了")

        host2 = _Host({}, memory=SimpleNamespace(backfill_memory_platform=boom))
        failed = asyncio.run(host2.api_memory_backfill_platform())
        self.assertEqual(failed["code"], 1)
        self.assertIn("回填炸了", failed["msg"])

    def test_lifecycle_preview_and_run(self) -> None:
        host = _Host({}, memory=SimpleNamespace(
            preview_lifecycle=lambda bot: {"dormant": 2},
            run_lifecycle=lambda bot, apply=True: {"expired": 1},
        ))
        preview = asyncio.run(host.api_lifecycle())
        self.assertEqual(preview["data"], {"dormant": 2})
        ran = asyncio.run(host.api_lifecycle_run())
        self.assertEqual(ran["data"], {"expired": 1})
        self.assertEqual(ran["msg"], "已整理")

    def test_memory_maintain_passes_apply_flag(self) -> None:
        compress_calls = []
        archive_calls = []

        async def compress(bot, apply=True):
            compress_calls.append((bot, apply))
            return {"merged_summaries": 1}

        async def archive(bot, days, ratio, apply=True):
            archive_calls.append((bot, days, ratio, apply))
            return {"archived": 2}

        mem = SimpleNamespace(compress_memories=compress, archive_stale=archive)
        host = _Host({"apply": "0"}, mem=mem)
        out = asyncio.run(host.api_memory_maintain())
        self.assertFalse(out["applied"])
        self.assertEqual(compress_calls, [("b", False)])
        self.assertEqual(archive_calls, [("b", 90, 0.3, False)])
        self.assertEqual(out["data"]["archive"], {"archived": 2})


class MessageHandlerTests(unittest.TestCase):
    def test_groups_lists_sessions_and_names(self) -> None:
        messages = SimpleNamespace(
            group_sessions=lambda bot, limit: [{"umo": "qq:1"}],
            group_name_overrides=lambda bot: {"qq:1": "家人群"},
        )
        out = asyncio.run(_Host({}, messages=messages).api_groups())
        self.assertEqual(out["data"]["items"], [{"umo": "qq:1"}])
        self.assertEqual(out["data"]["names"], {"qq:1": "家人群"})

    def test_group_rename_missing_and_both_results(self) -> None:
        host = _Host({}, messages=SimpleNamespace(rename_group=lambda *a: True))
        missing = asyncio.run(host.api_group_rename())
        self.assertEqual(missing["code"], 1)
        self.assertIn("umo", missing["msg"])

        host2 = _Host({"umo": "qq:1", "name": "家人群"},
                      messages=SimpleNamespace(rename_group=lambda bot, umo, name: True))
        saved = asyncio.run(host2.api_group_rename())
        self.assertEqual(saved["msg"], "已保存群名")

        host3 = _Host({"umo": "qq:1", "name": ""},
                      messages=SimpleNamespace(rename_group=lambda bot, umo, name: False))
        blank = asyncio.run(host3.api_group_rename())
        self.assertEqual(blank["code"], 1)
        self.assertEqual(blank["msg"], "名字不能为空")

    def test_messages_bad_limit_falls_back_to_100(self) -> None:
        captured = {}

        def grouped(bot, umo, limit, **kwargs):
            captured["limit"] = limit
            captured.update(kwargs)
            return []

        messages = SimpleNamespace(list_messages_grouped=grouped)
        out = asyncio.run(_Host({"limit": "abc"}, messages=messages).api_messages())
        self.assertEqual(out["code"], 0)
        self.assertEqual(captured["limit"], "abc")
        self.assertEqual(captured["summary_limit"], 100)


class RelationHandlerTests(unittest.TestCase):
    def test_profile_relation_overrides_and_appends(self) -> None:
        profiles = [
            {"uid": "u1", "name": "阿明", "relation": "恋人"},
            {"uid": "u2", "name": "小红", "relation": "朋友"},
            {"uid": "bot", "name": "你", "relation": "自己"},
            {"uid": "u3", "name": "小刚", "relation": "普通"},
        ]
        items = [
            {"a_uid": "bot", "b_uid": "u1", "relation": "熟人", "a_name": "", "b_name": ""},
            {"a_uid": "u1", "b_uid": "u3", "relation": "同事", "a_name": "", "b_name": ""},
        ]
        host = _Host({}, profiles=SimpleNamespace(list_profiles=lambda *a: profiles),
                     persons=SimpleNamespace(list_relations=lambda *a: list(items)))
        out = asyncio.run(host.api_relations())
        rows = out["data"]["items"]

        first = rows[0]
        self.assertEqual(first["relation"], "恋人")
        self.assertEqual(first["note"], "画像：关系字段")
        self.assertEqual(first["profile_relation"], "恋人")
        self.assertEqual(first["a_name"], "你")
        self.assertEqual(first["b_name"], "阿明")

        appended = [row for row in rows if row["b_uid"] == "u2"]
        self.assertEqual(len(appended), 1)
        self.assertEqual(appended[0]["a_uid"], "bot")
        self.assertEqual(appended[0]["b_name"], "小红")

        # 画像里 relation=普通 的不进图；bot 自己也不进
        self.assertFalse(any(row["b_uid"] == "u3" and row["a_uid"] == "bot" for row in rows))


class BaseMixinTests(unittest.TestCase):
    def test_amo_plugin_skips_failing_lookup(self) -> None:
        class _Ctx:
            def __init__(self):
                self.calls = []

            def get_registered_star(self, name):
                self.calls.append(name)
                if name == "Astrbot_amoupup_forever":
                    raise RuntimeError("旧名字没注册")
                return SimpleNamespace(star_cls="amo-star")

        host = _Host()
        host.context = _Ctx()
        self.assertEqual(host._amo_plugin(), "amo-star")
        self.assertEqual(host.context.calls,
                         ["Astrbot_amoupup_forever", "Astrbot_Amo_forever"])

    def test_amo_plugin_swallows_context_errors(self) -> None:
        class _BadCtx:
            def __getattr__(self, name):
                raise RuntimeError("context 坏了")

        host = _Host()
        host.context = _BadCtx()
        self.assertIsNone(host._amo_plugin())

    def test_forget_in_amo_success_and_failure(self) -> None:
        removed = []
        db = SimpleNamespace(delete_profile=lambda uid: removed.append(uid))
        host = _Host()
        host._amo_plugin = lambda: SimpleNamespace(db=db)
        self.assertTrue(host._forget_in_amo("u1"))
        self.assertEqual(removed, ["u1"])

        def boom(uid):
            raise RuntimeError("删不掉")

        host2 = _Host()
        host2._amo_plugin = lambda: SimpleNamespace(db=SimpleNamespace(delete_profile=boom))
        self.assertFalse(host2._forget_in_amo("u2"))

    def test_body_falls_back_to_get_json(self) -> None:
        class _BadJson:
            async def json(self, default=None):
                raise RuntimeError("json 解析失败")

            async def get_json(self, silent=True):
                return {"uid": "u"}

        host = _Host()
        with mock.patch.object(base_module, "web_request", _BadJson()):
            out = asyncio.run(HandlerBaseMixin._body(host))
        self.assertEqual(out, {"uid": "u"})

    def test_body_returns_empty_without_request(self) -> None:
        host = _Host()
        with mock.patch.object(base_module, "web_request", None):
            out = asyncio.run(HandlerBaseMixin._body(host))
        self.assertEqual(out, {})

    def test_body_ignores_non_dict_json(self) -> None:
        class _ListJson:
            async def json(self, default=None):
                return ["不是字典"]

        host = _Host()
        with mock.patch.object(base_module, "web_request", _ListJson()):
            out = asyncio.run(HandlerBaseMixin._body(host))
        self.assertEqual(out, {})


class HandlersPackageFallbackTests(unittest.TestCase):
    def test_top_level_import_falls_back_to_absolute(self) -> None:
        path = os.path.join(ROOT, "core", "handlers", "__init__.py")
        spec = importlib.util.spec_from_file_location("handlers_pkg_cov", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(issubclass(module.HandlerBaseMixin, HandlerBaseMixin))
        self.assertEqual(set(module.__all__), {
            "HandlerBaseMixin", "ProfileHandlersMixin", "RelationHandlersMixin",
            "MemoryHandlersMixin", "MessageHandlersMixin", "SystemHandlersMixin",
        })
        self.assertNotIn("handlers_pkg_cov", sys.modules)


if __name__ == "__main__":
    unittest.main()
