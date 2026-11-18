# -*- coding: utf-8 -*-
"""core/handlers/__init__.py —— 34 条面板接口按域拆开后的组合入口。

拆分（Phase 13 P2，单文件 <=300 行）：
    base.py      公共底座（读请求体 / 陪伴侧同步删画像）
    profiles.py  画像 + 人物（9 条）
    relations.py 关系图（3 条）
    memory.py    记忆（5 条）
    messages.py  聊天记录 / 群（5 条）
    system.py    状态 / 备份 / 日志 / 缓存 / 维护（12 条）
路由与 methods 仍由 core/web_api.py 的 WebAPIMixin 注册，path/methods 未变。
"""
from __future__ import annotations

try:  # 包模式
    from .base import HandlerBaseMixin
    from .memory import MemoryHandlersMixin
    from .messages import MessageHandlersMixin
    from .profiles import ProfileHandlersMixin
    from .relations import RelationHandlersMixin
    from .system import SystemHandlersMixin
except ImportError:  # 顶层导入（测试/脚本）
    from core.handlers.base import HandlerBaseMixin
    from core.handlers.memory import MemoryHandlersMixin
    from core.handlers.messages import MessageHandlersMixin
    from core.handlers.profiles import ProfileHandlersMixin
    from core.handlers.relations import RelationHandlersMixin
    from core.handlers.system import SystemHandlersMixin

__all__ = [
    "HandlerBaseMixin", "ProfileHandlersMixin", "RelationHandlersMixin",
    "MemoryHandlersMixin", "MessageHandlersMixin", "SystemHandlersMixin",
]
