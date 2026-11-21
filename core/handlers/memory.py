# -*- coding: utf-8 -*-
"""core/handlers/memory.py —— 记忆接口（/memories /memory/save /memory/delete /memory/maintain …）。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

try:  # 包模式
    from ..web_response import fail, flag_field, int_field, missing, ok, text_field
except ImportError:  # 顶层导入（测试）
    from core.web_response import fail, flag_field, int_field, missing, ok, text_field


class MemoryHandlersMixin:
    """记忆读写与整理：列表/检索、手写、删除、手动整理、来源回填。"""

    if TYPE_CHECKING:  # 宿主属性/方法由 main.py 的其他模块提供
        _body: Any
        _bot_id: Any
        mem: Any
        memory: Any
        retrieval: Any

    async def api_memories(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        uid = text_field(payload, "uid")
        query = text_field(payload, "q")
        tag = text_field(payload, "tag", "tags")
        if query or tag:
            # recall 开了语义召回会走 HTTP（embedding）；面板是 async 处理函数，
            # 同步调用会卡住事件循环，最多 embedding_timeout 秒。丢线程池里跑。
            # tag 非空时只在带这个标签的记忆里查（如「#活动 event:<id>」）。
            items = (await asyncio.to_thread(self.retrieval.recall, bot_id, uid, query, 100,
                                            tags=tag)) if uid else []
        else:
            items = self.memory.list_memories(bot_id, uid, 200)
        return ok({"items": items})

    async def api_memory_save(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        uid = text_field(payload, "uid")
        content = text_field(payload, "content")
        if not uid or not content:
            return fail("uid 和 content 不能为空")
        saved = self.memory.remember(
            bot_id, uid, content,
            kind=str(payload.get("kind") or "manual"),
            importance=payload.get("importance", 0.6),
            scope=str(payload.get("scope") or "private"),
            group_id=str(payload.get("group_id") or ""),
            platform=str(payload.get("platform") or ""),
        )
        if saved.get("ok"):
            return ok(saved, "已记住")
        return fail("已记住", data=saved)

    async def api_memory_backfill_platform(self):
        """一次性回填：给 platform 为空的老记忆补来源平台（只补能确定的）。"""
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        payload_apply = flag_field(payload, "apply", True)
        try:
            result = await asyncio.to_thread(
                self.memory.backfill_memory_platform, bot_id, 600, 20000, payload_apply,
            )
        except Exception as exc:
            return fail(str(exc))
        return ok(result, applied=payload_apply)

    async def api_lifecycle(self):
        """复盘：按当前 TTL 算会休眠/过期多少条（dry-run，不改库）。"""
        payload = await self._body()
        bot_id = self._bot_id(payload)
        data = await asyncio.to_thread(self.memory.preview_lifecycle, bot_id)
        return ok(data)

    async def api_lifecycle_run(self):
        """立即跑一次生命周期整理（写接口，面板手动按钮用）。"""
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        data = await asyncio.to_thread(self.memory.run_lifecycle, bot_id, True)
        return ok(data, "已整理")

    async def api_memory_delete(self):
        payload = await self._body()
        memory_id = int_field(payload, "id")
        if not memory_id:
            return missing("id")
        # 面板只传 id（不带 bot_id）：先按 id 反查这条记忆属于哪个 bot，
        # 否则多 bot 库里会拿错 bot_id，DELETE 静默删 0 条。
        bot_id = text_field(payload, "bot_id")
        if not bot_id:
            bot_id = self.memory.bot_of_memory(memory_id) or self._bot_id(payload, write=True)
        return ok({"deleted": self.memory.delete_memory(bot_id, memory_id)})

    async def api_memory_maintain(self):
        """面板/脚本手动触发整理；payload: {apply: false} 可先预演。"""
        payload = await self._body()
        bot_id = self._bot_id(payload)
        apply = flag_field(payload, "apply", True)
        out = {"compress": await self.mem.compress_memories(bot_id, apply=apply),
               "archive": await self.mem.archive_stale(bot_id, 90, 0.3, apply=apply)}
        return ok(out, applied=apply)
