# -*- coding: utf-8 -*-
"""services/fact_rules.py —— 事实级压缩的纯规则（不碰 DB、不读 settings）。

fact_dedupe 负责「句子 → 事实键/状态」的归一；这里负责把一批行按事实分组、
正负状态切开、拼出合并正文、算提升后的重要度。compression_engine 只留 SQL 与写回。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # 包模式
    from ..services import fact_dedupe
except ImportError:  # 顶层导入（测试/脚本）
    from services import fact_dedupe


def group_static_facts(rows: Sequence[Any]) -> Dict[Tuple[str, str, str, str], List[Any]]:
    """只把静态事实（偏好/人设/关系/资产）按「谁 + 范围 + 会话 + 事实键」分组。"""
    groups: Dict[Tuple[str, str, str, str], List[Any]] = {}
    for row in rows:
        kind = str(row["kind"] or "")
        if not fact_dedupe.is_static_kind(kind):
            continue
        key = fact_dedupe.fact_key(str(row["content"] or ""))
        if not key:
            continue
        gkey = (str(row["uid"] or ""), str(row["scope"] or ""),
                str(row["group_id"] or ""), key)
        groups.setdefault(gkey, []).append(row)
    return groups


def split_neg_pos(items: Sequence[Any]) -> Tuple[List[Any], List[Any]]:
    """按事实状态切开：(否定/反转行, 其余行)。判定与原来完全一致（r not in neg）。"""
    neg = [r for r in items if fact_dedupe.fact_state(str(r["content"] or "")) == "neg"]
    pos = [r for r in items if r not in neg]
    return neg, pos


def newest(rows: Sequence[Any]) -> Optional[Any]:
    """取 created_at 最大的那条（同值取先出现的，与 max() 语义一致）。"""
    return max(rows, key=lambda r: int(r["created_at"] or 0)) if rows else None


def pick_base(items: Sequence[Any]) -> Any:
    """合并基准行：重要度最高，其次正文最长（与 max(..., key=...) 一致）。"""
    return max(items, key=lambda r: (float(r["importance"] or 0),
                                    len(str(r["content"] or ""))))


def merged_text(items: Sequence[Any], base: Any) -> str:
    """把同组其它事实的细节并进基准行正文。"""
    text = str(base["content"] or "")
    for other in items:
        if other is base:
            continue
        text = fact_dedupe.merge_fact(text, str(other["content"] or ""))
    return text


def bumped_importance(items: Sequence[Any]) -> float:
    """合并后重要度：最高值 + 每多一条 +0.05，封顶 1.0。"""
    top = max(float(r["importance"] or 0) for r in items)
    return min(1.0, top + 0.05 * (len(items) - 1))


def decay_targets(rows: Sequence[Any], cutoff: int, floor_importance: float = 0.2):
    """遗忘曲线候选：静态事实、60 天没被检索、重要度还高于下限的行。"""
    targets = []
    for row in rows:
        if not fact_dedupe.is_static_kind(str(row["kind"] or "")):
            continue
        if int(row["last_hit_at"] or 0) > cutoff:
            continue
        importance = float(row["importance"] or 0)
        if importance <= floor_importance:
            continue
        targets.append((importance, row))
    return targets
