# -*- coding: utf-8 -*-
"""services/consolidation.py —— 旧流水巩固：同一天同一会话的多条流水并成一条。

只做纯计划（不碰 DB）：把「7 天前的 activity/event 流水按天分组，≥3 条」合并成
一条「MM-DD 流水：A；B；C」，原文一字不丢（用「；」拼），由 compression_engine 落库。
巩固后的记忆挂 merged 状态，不再参与召回；重要的单条（importance≥0.7）不并。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Sequence

#: 参与巩固的 kind（和 lifecycle 的流水集合一致；摘要/事实不动）
NOISE_KINDS = {
    "activity", "event", "bot_media", "proactive_message", "self_action",
    "tool_memory", "social_news", "work",
}

CAP_CHARS = 500


def _day(ts: Any) -> str:
    try:
        return time.strftime("%m-%d", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return "--"


def plan(rows: Sequence[Dict[str, Any]], now: int, days: float = 7,
         min_items: int = 3) -> List[Dict[str, Any]]:
    """返回 [{keep_id, ids, content, importance}]；不足 min_items 的组不动。"""
    cutoff = int(now) - max(0.0, float(days)) * 86400
    groups: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows:
        try:
            if str(row.get("kind") or "") not in NOISE_KINDS:
                continue
            if int(row.get("created_at") or 0) >= cutoff:
                continue
            if float(row.get("importance") or 0) >= 0.7:
                continue
        except (TypeError, ValueError):
            continue
        key = (str(row.get("uid") or ""), str(row.get("scope") or "private"),
               str(row.get("group_id") or ""), _day(row.get("created_at")))
        groups.setdefault(key, []).append(row)
    out: List[Dict[str, Any]] = []
    for (uid, scope, group_id, day), items in groups.items():
        if len(items) < max(2, int(min_items)):
            continue
        items.sort(key=lambda r: (float(r.get("importance") or 0), int(r.get("id") or 0)))
        keep = items[-1]
        body = "；".join(str(r.get("content") or "").strip() for r in items)
        content = ("%s 流水：%s" % (day, body))[:CAP_CHARS]
        out.append({
            "keep_id": int(keep.get("id") or 0),
            "ids": [int(r.get("id") or 0) for r in items],
            "content": content,
            "importance": max(float(r.get("importance") or 0) for r in items),
            "uid": uid, "scope": scope, "group_id": group_id,
        })
    return out
