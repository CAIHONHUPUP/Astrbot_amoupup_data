# -*- coding: utf-8 -*-
"""services/memory_decay.py —— 按记忆类型决定「多快变旧」和「多久归档」。

原来所有记忆共用 45 天半衰期、90 天归档：一句「今天吃了面」和「主人对芒果过敏」
衰减速度一样，流水账会一直和长期事实抢召回。这里按 kind 分开：

    * 流水账 / 机器写的（activity、event、bot_media…）活得最短；
    * 摘要类活得中等（周摘要、日总结要能翻旧账）；
    * 事实 / 偏好 / 人设 / 里程碑接近永久。

纯函数，不碰 DB；归档 SQL 在 models/dao.py。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Set

#: 流水账类：进归档候选时用更短的 TTL（天）
NOISE_TTL_DAYS = 30
#: 流水账归档的重要度上限（重要的活动记录不提前归档）
NOISE_MAX_IMPORTANCE = 0.4
#: 流水账 kind 集合：这里是「该快速衰减/优先归档」的口径，比
#: core/memory_tooling.NOISY_KINDS（只标「机器写的、别当人话」那 5 种）更宽——
#: activity/event/work 这些要衰减，但不是机器旁白。两边**故意不同**，别硬拉平。
NOISE_KINDS: Set[str] = {
    "activity", "event", "chat", "message", "bot_media", "proactive_message",
    "self_action", "tool_memory", "social_news", "work",
    "imported_memory",
}

#: 每种 kind 的半衰期（天）；没列出的用配置里的默认值
KIND_HALF_LIFE_DAYS: Dict[str, float] = {
    "activity": 7.0,
    "event": 14.0,
    "imported_memory": 21.0,
    "bot_media": 7.0,
    "proactive_message": 7.0,
    "self_action": 7.0,
    "tool_memory": 14.0,
    "social_news": 7.0,
    "work": 21.0,
    "daily_digest": 120.0,
    "conversation_summary": 90.0,
    "milestone": 365.0,
    "important_event": 365.0,
    "relationship_phase_summary": 365.0,
    "fact": 3650.0,
    "stable_fact": 3650.0,
    "preference": 3650.0,
    "core_memory": 3650.0,
    "assertion": 3650.0,
    "place": 3650.0,
}


def half_life_for(kind: Any, default: float) -> float:
    """这个 kind 的遗忘半衰期；未配置的 kind 用 default。"""
    try:
        base = float(default)
    except (TypeError, ValueError):
        base = 45.0
    if base <= 0:
        base = 45.0
    return float(KIND_HALF_LIFE_DAYS.get(str(kind or "").strip().lower(), base))


def is_noise(kind: Any) -> bool:
    return str(kind or "").strip().lower() in NOISE_KINDS


def noise_kind_list(extra: Iterable[str] = ()) -> list:
    """归档 SQL 的 kind 列表（排序保证可测）。"""
    items = set(NOISE_KINDS)
    items.update(str(kind) for kind in extra if kind)
    return sorted(items)
