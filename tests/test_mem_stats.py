# -*- coding: utf-8 -*-
"""/mem_stats：只读统计；失败也不能把错误抛回聊天。"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from main import AmoDataPlugin  # noqa: E402


class FakeMem:
    async def stats(self, bot):
        assert bot == ""
        return {"memories": 7, "memories_active": 5, "profiles": 2,
                "persons": 3, "messages": 9, "relations": 1}


class BoomMem:
    async def stats(self, bot):
        raise RuntimeError("db locked")


class FakeEvent:
    def plain_result(self, text):
        return text


async def _collect(plugin):
    return [item async for item in plugin.mem_stats_cmd(FakeEvent())]


def _plugin(mem):
    plugin = object.__new__(AmoDataPlugin)
    plugin.mem = mem
    plugin._bot_id = lambda *a, **k: ""
    return plugin


def test_mem_stats_formats_counts():
    out = asyncio.run(_collect(_plugin(FakeMem())))
    assert len(out) == 1
    for frag in ("记忆 7 条", "活跃 5", "画像 2", "人物 3", "消息 9", "关系 1"):
        assert frag in out[0], out[0]


def test_mem_stats_never_raises():
    out = asyncio.run(_collect(_plugin(BoomMem())))
    assert out and "失败" in out[0]
