# -*- coding: utf-8 -*-
"""覆盖率补齐：core/api.py 里第一批没走到的分支。

* consolidate_semantic：模块缺失 / 开关关闭 / 拿不到 provider / 空聚类 /
  逐组合并（一次成功、一次调用炸、一次合并被拒）；
* _maybe_rerank：拿 provider 失败 / 正常重排；
* remember：引擎取 remember 属性炸掉时回退插件方法、引擎没有 remember 时回退插件方法；
* correct / nightly_memory_candidates / nightly_apply 的异常与脏数据分支。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from core import api as api_module
from core.api import MemoryAPI

BOT = "b"


def _boom(*args, **kwargs):
    raise RuntimeError("boom")


class _Provider:
    def __init__(self, replies=None):
        self.replies = list(replies or [])

    async def text_chat(self, prompt, system_prompt=""):
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(completion_text=value)


class ConsolidateSemanticTests(unittest.TestCase):
    def test_module_missing_returns_reason(self) -> None:
        api = MemoryAPI(SimpleNamespace(settings={}))
        with mock.patch.object(api_module, "semantic_consolidation", None):
            out = asyncio.run(api.consolidate_semantic(BOT))
        self.assertEqual(out, {"ok": False, "reason": "module_missing"})

    def test_disabled_by_settings(self) -> None:
        api = MemoryAPI(SimpleNamespace(settings={"semantic_consolidation_enabled": False}))
        out = asyncio.run(api.consolidate_semantic(BOT))
        self.assertEqual(out, {"ok": False, "reason": "disabled"})

    def test_provider_error_reports_no_provider(self) -> None:
        plugin = SimpleNamespace(
            settings={"semantic_consolidation_enabled": True},
            context=SimpleNamespace(get_using_provider=_boom),
            memory=SimpleNamespace(semantic_clusters=lambda bot: []),
        )
        out = asyncio.run(MemoryAPI(plugin).consolidate_semantic(BOT))
        self.assertEqual(out, {"ok": False, "reason": "no_provider"})

    def test_empty_clusters_returns_zero(self) -> None:
        plugin = SimpleNamespace(
            settings={"semantic_consolidation_enabled": True},
            context=SimpleNamespace(get_using_provider=lambda: object()),
            memory=SimpleNamespace(semantic_clusters=lambda bot: []),
        )
        out = asyncio.run(MemoryAPI(plugin).consolidate_semantic(BOT))
        self.assertEqual(out, {"ok": True, "groups": 0, "merged": 0})

    def test_merges_and_skips_bad_groups(self) -> None:
        clusters = [
            {"contents": ["a", "b"], "keep_id": 1, "ids": [1, 2]},
            {"contents": ["c"], "keep_id": 3, "ids": [3]},
            {"contents": ["d"], "keep_id": 4, "ids": [4]},
        ]
        provider = _Provider(["merged-A", RuntimeError("chat boom"), "rejected"])
        merged_calls = []

        def apply_merge(keep_id, merged, ids):
            merged_calls.append((keep_id, merged, ids))
            return 2

        fake_module = SimpleNamespace(
            build_prompt=lambda contents: "合并这些：" + ",".join(contents),
            accept_merged=lambda merged, contents: str(merged).startswith("merged"),
        )
        plugin = SimpleNamespace(
            settings={
                "semantic_consolidation_enabled": True,
                "semantic_consolidation_max_groups": 3,
                "semantic_consolidation_timeout": 5,
            },
            context=SimpleNamespace(get_using_provider=lambda: provider),
            memory=SimpleNamespace(
                semantic_clusters=lambda bot: clusters,
                apply_semantic_merge=apply_merge,
            ),
        )
        with mock.patch.object(api_module, "semantic_consolidation", fake_module):
            out = asyncio.run(MemoryAPI(plugin).consolidate_semantic(BOT))

        self.assertEqual(out, {"ok": True, "groups": 3, "done": 1, "merged": 2})
        self.assertEqual(merged_calls, [(1, "merged-A", [1, 2])])


class RerankTests(unittest.TestCase):
    def test_provider_lookup_failure_returns_items(self) -> None:
        plugin = SimpleNamespace(
            settings={"recall_rerank_enabled": True},
            context=SimpleNamespace(get_using_provider=_boom),
        )
        items = [{"id": 1}, {"id": 2}]
        out = asyncio.run(MemoryAPI(plugin)._maybe_rerank(items, "q", 5))
        self.assertEqual(out, items)

    def test_rerank_trims_to_limit(self) -> None:
        items = [{"id": 1}, {"id": 2}, {"id": 3}]
        calls = []

        class _Rerank:
            async def rerank(self, rows, query, limit, call, max_candidates, timeout):
                calls.append((query, limit, max_candidates, timeout))
                text = await call("排序")
                self.text = text
                return list(reversed(rows))

        rerank = _Rerank()
        plugin = SimpleNamespace(
            settings={
                "recall_rerank_enabled": True,
                "recall_rerank_candidates": 7,
                "recall_rerank_timeout": 2.5,
            },
            context=SimpleNamespace(get_using_provider=lambda: _Provider(["{}"])),
        )
        with mock.patch.object(api_module, "recall_rerank", rerank):
            out = asyncio.run(MemoryAPI(plugin)._maybe_rerank(items, "q", 2))

        self.assertEqual(out, [{"id": 3}, {"id": 2}])
        self.assertEqual(calls, [("q", 2, 7, 2.5)])
        self.assertEqual(rerank.text, "{}")


class RememberFallbackTests(unittest.TestCase):
    def test_getattr_failure_falls_back_to_plugin_method(self) -> None:
        class _WeirdEngine:
            def __getattribute__(self, name):
                if name == "remember":
                    raise RuntimeError("attr boom")
                return object.__getattribute__(self, name)

        captured = {}

        def remember(bot, uid, content, **kwargs):
            captured["args"] = (bot, uid, content)
            captured["kwargs"] = kwargs
            return {"ok": True}

        plugin = SimpleNamespace(settings={}, memory=_WeirdEngine(), remember=remember)
        out = asyncio.run(MemoryAPI(plugin).remember("b", "u", "内容"))
        self.assertTrue(out["ok"])
        self.assertEqual(captured["args"], ("b", "u", "内容"))

    def test_engine_without_remember_uses_plugin_method(self) -> None:
        def remember(bot, uid, content, **kwargs):
            return {"ok": True, "memory_id": 3}

        plugin = SimpleNamespace(settings={}, memory=SimpleNamespace(), remember=remember)
        out = asyncio.run(MemoryAPI(plugin).remember("b", "u", "内容", kind="fact"))
        self.assertEqual(out["memory_id"], 3)


class NightlyAndCorrectTests(unittest.TestCase):
    def test_correct_swallows_errors(self) -> None:
        plugin = SimpleNamespace(memory=SimpleNamespace(correct=_boom))
        out = asyncio.run(MemoryAPI(plugin).correct("b", "u", "记错了", "对的"))
        self.assertFalse(out["ok"])
        self.assertIn("boom", out["reason"])

    def test_nightly_candidates_swallows_errors(self) -> None:
        plugin = SimpleNamespace(memory=SimpleNamespace(nightly_candidates=_boom))
        out = asyncio.run(MemoryAPI(plugin).nightly_memory_candidates(12, 50))
        self.assertEqual(out, [])

    def test_nightly_apply_handles_bad_facts_and_ids(self) -> None:
        archive_calls = []
        remembers = []

        def remember(bot, uid, text, **kwargs):
            remembers.append(text)
            if text == "写不进去":
                raise RuntimeError("write boom")
            if text == "脏 id":
                return {"ok": True, "memory_id": "not-a-number"}
            return {"ok": True, "memory_id": 5}

        def archive(keep, bot, uid):
            archive_calls.append((keep, bot, uid))
            raise RuntimeError("archive boom")

        plugin = SimpleNamespace(memory=SimpleNamespace(remember=remember, nightly_archive=archive))
        meta = {"bot_id": BOT, "uid": "u", "scope": "private",
                "group_id": "g", "subject": "user"}
        facts = ["", "写不进去", "脏 id", "普通事实"]
        ids = [0, "x", 5, 7]
        out = asyncio.run(MemoryAPI(plugin).nightly_apply(meta, facts, ids))

        self.assertEqual(out, {"written": 2, "archived": 0})
        self.assertEqual(remembers, ["写不进去", "脏 id", "普通事实"])
        self.assertEqual(archive_calls, [([7], BOT, "u")])

    def test_nightly_apply_without_writes_skips_archive(self) -> None:
        plugin = SimpleNamespace(
            memory=SimpleNamespace(
                remember=lambda *a, **k: {"ok": False, "reason": "x"},
                nightly_archive=lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该归档")),
            )
        )
        out = asyncio.run(MemoryAPI(plugin).nightly_apply({"bot_id": BOT}, ["事实"], [1, 2]))
        self.assertEqual(out, {"written": 0, "archived": 0})


if __name__ == "__main__":
    unittest.main()
