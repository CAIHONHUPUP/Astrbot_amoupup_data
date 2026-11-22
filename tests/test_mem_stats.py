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

from plugin_fakes import MemStatsFakeMem as FakeMem  # noqa: E402

class BoomMem:
    async def stats(self, bot):
        raise RuntimeError("db locked")

# Phase 13 P2：事件桩搬到 tests/fixtures.py（只搬位置，断言不变）
from fixtures import StubEvent as FakeEvent  # noqa: E402

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
    # task-27：记忆给“活跃/总数”，聊天只算真实对话、摘要单列
    for frag in ("记忆 5 条", "活跃 5 / 共 7", "画像 2", "人物 3",
                 "聊天 9", "摘要 0 单列", "关系 1"):
        assert frag in out[0], out[0]

def test_mem_stats_never_raises():
    out = asyncio.run(_collect(_plugin(BoomMem())))
    assert out and "失败" in out[0]