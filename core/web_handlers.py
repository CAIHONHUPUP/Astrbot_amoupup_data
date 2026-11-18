# -*- coding: utf-8 -*-
"""core/web_handlers.py —— 面板 handler 组合入口（34 条接口按域在 core/handlers/ 下）。

这里只做组合，不再堆 handler 主体：
    画像/人物  core/handlers/profiles.py       关系  core/handlers/relations.py
    记忆       core/handlers/memory.py         聊天  core/handlers/messages.py
    系统       core/handlers/system.py         底座  core/handlers/base.py
路由 path / methods 与写接口守卫仍在 core/web_api.py，未改动。
"""
from __future__ import annotations

try:  # 包模式
    from .handlers import (HandlerBaseMixin, MemoryHandlersMixin, MessageHandlersMixin,
                           ProfileHandlersMixin, RelationHandlersMixin, SystemHandlersMixin)
except ImportError:  # 顶层导入（测试）
    from core.handlers import (HandlerBaseMixin, MemoryHandlersMixin, MessageHandlersMixin,
                               ProfileHandlersMixin, RelationHandlersMixin, SystemHandlersMixin)

__all__ = [
    "WebHandlersMixin", "HandlerBaseMixin", "ProfileHandlersMixin", "RelationHandlersMixin",
    "MemoryHandlersMixin", "MessageHandlersMixin", "SystemHandlersMixin",
]


class WebHandlersMixin(HandlerBaseMixin, ProfileHandlersMixin, RelationHandlersMixin,
                       MemoryHandlersMixin, MessageHandlersMixin, SystemHandlersMixin):
    """34 条面板接口（按域拆分后组合；写操作只收 POST，由 WebAPIMixin 守卫）。"""
