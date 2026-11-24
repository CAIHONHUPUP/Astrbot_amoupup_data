# -*- coding: utf-8 -*-
"""services/lifecycle.py —— 记忆生命周期：按 kind 的休眠/过期（纯规则）。

和 memory_decay（评分用的半衰期）分开：这里决定「什么时候退出召回」——
流水账 30 天不碰就休眠、一年后过期；摘要 180 天休眠；事实/偏好/里程碑永不休眠。
休眠不是删除：强关键词命中时会复活（retrieval._merge_dormant），过期只标记不删正文。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

#: kind -> 休眠天数（None/缺省 = 不自动休眠）
KIND_DORMANT_DAYS: Dict[str, Optional[float]] = {
    "activity": 30.0,
    "event": 45.0,
    "imported_memory": None,   # 迁移来的老记忆来源不明，不自动休眠（宁可多留）
    "bot_media": 30.0,
    "proactive_message": 30.0,
    "self_action": 30.0,
    "tool_memory": 45.0,
    "social_news": 30.0,
    "work": 60.0,
    "finish": 30.0,
    "conversation_summary": 180.0,
    "daily_digest": 180.0,
    "relationship_phase_summary": 180.0,
    "fact": None,
    "stable_fact": None,
    "preference": None,
    "core_memory": None,
    "milestone": None,
    "important_event": None,
    "place": None,
    "assertion": 365.0,
}

#: kind -> 过期天数（只标记 status='expired'，不删正文）
KIND_EXPIRE_DAYS: Dict[str, Optional[float]] = {
    "activity": 365.0,
    "event": 365.0,
    "bot_media": 180.0,
    "proactive_message": 365.0,
    "self_action": 365.0,
    "social_news": 180.0,
}

#: 默认（未知 kind）：一年休眠、不自动过期
DEFAULT_DORMANT_DAYS = 365.0


SUMMARY_KINDS = {"conversation_summary", "daily_digest", "relationship_phase_summary"}


def should_run(last_run: int, now: int, min_hours: float = 20.0) -> bool:
    """错过补偿：距上次够久就跑（进程停过几天也照跑一次，plan 天然幂等）。"""
    try:
        last = int(last_run or 0)
        now = int(now or 0)
    except (TypeError, ValueError):
        return True
    if not last:
        return True
    return (now - last) >= max(1.0, float(min_hours)) * 3600


def dormant_days_for(kind: Any, overrides: Any = None) -> Optional[float]:
    """kind 的休眠天数；overrides 支持 noise_days / summary_days 两档全局覆盖。"""
    key = str(kind or "").strip().lower()
    base = KIND_DORMANT_DAYS.get(key, DEFAULT_DORMANT_DAYS)
    if not overrides:
        return base
    if key in SUMMARY_KINDS and overrides.get("summary_days") is not None:
        return float(overrides["summary_days"])
    if key not in SUMMARY_KINDS and overrides.get("noise_days") is not None:
        if key in KIND_DORMANT_DAYS and KIND_DORMANT_DAYS[key] is None:
            return None
        return float(overrides["noise_days"])
    return base


def expire_days_for(kind: Any, overrides: Any = None) -> Optional[float]:
    key = str(kind or "").strip().lower()
    base = KIND_EXPIRE_DAYS.get(key)
    if overrides and overrides.get("expire_days") is not None and base is not None:
        return float(overrides["expire_days"])
    return base


def _age_days(row: Dict[str, Any], now: int) -> float:
    last = row.get("last_hit_at") or row.get("created_at") or now
    try:
        return max(0.0, (int(now) - int(last)) / 86400.0)
    except (TypeError, ValueError):
        return 0.0


def plan(rows: Sequence[Dict[str, Any]], now: int,
         overrides: Any = None) -> Tuple[List[int], List[int]]:
    """返回 (休眠 ids, 过期 ids)；重要/被反复想起的记忆不动。"""
    dormant: List[int] = []
    expired: List[int] = []
    for row in rows:
        try:
            memory_id = int(row["id"])
            importance = float(row.get("importance") or 0.5)
            hits = int(row.get("hit_count") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        age = _age_days(row, now)
        expire_after = expire_days_for(row.get("kind"), overrides)
        if expire_after is not None and age >= expire_after:
            expired.append(memory_id)
            continue
        dormant_after = dormant_days_for(row.get("kind"), overrides)
        if dormant_after is None or age < dormant_after:
            continue
        if importance >= 0.75 or hits >= 3:
            continue
        dormant.append(memory_id)
    return dormant, expired
