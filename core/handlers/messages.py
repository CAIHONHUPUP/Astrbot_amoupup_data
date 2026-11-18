# -*- coding: utf-8 -*-
"""core/handlers/messages.py —— 聊天记录接口（/messages /message/* /groups /group/rename）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:  # 包模式
    from ..web_response import confirm_required, confirmed, fail, int_field, missing, ok, result, text_field
except ImportError:  # 顶层导入（测试）
    from core.web_response import (confirm_required, confirmed, fail, int_field, missing, ok,
                                   result, text_field)


class MessageHandlersMixin:
    """聊天记录：分页/按时间回看、删除、清空、群列表与群名。"""

    if TYPE_CHECKING:  # 宿主属性/方法由 main.py 的其他模块提供
        _body: Any
        _bot_id: Any
        mem: Any
        messages: Any

    async def api_groups(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        return ok({
            "items": self.messages.group_sessions(bot_id, 500),
            "names": self.messages.group_name_overrides(bot_id),
        })

    async def api_group_rename(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        umo = text_field(payload, "umo")
        name = text_field(payload, "name")
        if not umo:
            return missing("umo")
        renamed = self.messages.rename_group(bot_id, umo, name)
        return result(renamed, None, "已保存群名", "名字不能为空")

    async def api_messages(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        umo = text_field(payload, "umo")
        limit = payload.get("limit", 200)
        pid = int_field(payload, "pid")
        platform = text_field(payload, "platform")
        uid = text_field(payload, "uid")
        kind = text_field(payload, "kind").lower()
        query = text_field(payload, "query")
        if query:
            # 带时间说法（昨天下午 / 上周三 / 11月6号晚上10点）就走分层检索：
            # 年 → 月日 → 小时 → 具体消息，正文不带时间戳
            found = await self.mem.recall_messages(
                bot_id, query=query, umo=umo, uid=uid, limit=limit
            )
            return ok(found)
        try:
            summary_limit = max(20, min(200, int(limit)))
        except (TypeError, ValueError):
            summary_limit = 100
        return ok({
            "items": self.messages.list_messages_grouped(
                bot_id, umo, limit, pid=pid, platform=platform, uid=uid,
                summary_limit=summary_limit, kind=kind,
            )
        })

    async def api_message_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        message_id = int_field(payload, "id")
        if not message_id:
            return missing("id")
        return ok({"deleted": self.messages.delete_message(bot_id, message_id)})

    async def api_message_clear(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        umo = text_field(payload, "umo")
        if not confirmed(payload):
            return confirm_required("清空")
        # 缺 umo 时绝不能当成“清空全部”，那是不可恢复的整库删聊天
        if not umo and str(payload.get("all") or "") not in ("1", "true", "yes"):
            return fail("必须指定 umo；清空全部请显式传 all=1")
        return ok({"deleted": self.messages.clear_messages(bot_id, umo)})
