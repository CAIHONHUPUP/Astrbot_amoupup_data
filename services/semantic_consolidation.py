# -*- coding: utf-8 -*-
"""services/semantic_consolidation.py —— 跨天同主题的语义巩固（纯规则 + 可选模型）。

规则版巩固（services/consolidation.py）只并「同一天流水」；这一层处理跨天同主题：
用二元组 Jaccard 把同一用户/会话/同 kind 的记忆聚成组，再交一次小模型合并成一句事实。
纯规则部分可离线单测；模型调用由 core/api.py 编排（拿不到 provider 就跳过）。
"""
from __future__ import annotations

import os
import re
from itertools import combinations
from typing import Any, Dict, List, Sequence, Set

#: 可以参与语义巩固的 kind（流水有专门的同天巩固，这里主要处理事实/偏好/事件）
MERGEABLE_KINDS = {
    "fact", "stable_fact", "preference", "event", "important_event",
    "assertion", "place", "work",
}
MIN_ITEMS = 2
MAX_ITEMS = 6
MAX_GROUPS = 3
MIN_JACCARD = 0.5
MIN_CHARS = 8
MAX_CHARS = 300
#: 机器/模板写的前缀：剥掉再比，避免「今天主动跟老公分享：」这种共同开头把不同的事并到一起
_BOILERPLATE = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}\s*)?"
    r"(?:今天)?主动(?:跟|和)(?:老公|你|他)?分享[：:]\s*"
)


def _core(text: Any) -> str:
    """去掉日期/「主动分享：」模板前缀后的正文。"""
    body = _BOILERPLATE.sub("", str(text or "").strip())
    return body.strip() or str(text or "").strip()


def _shared_prefix_ratio(left: str, right: str) -> float:
    shorter = min(len(left), len(right))
    if not shorter:
        return 0.0
    prefix = os.path.commonprefix([left, right])
    return len(prefix) / float(shorter)


def _tokens(text: Any) -> Set[str]:
    try:
        from ..utils.text_process import _informative_terms
    except ImportError:  # 顶层导入（测试/脚本）
        from utils.text_process import _informative_terms
    return {term for term in _informative_terms(_core(text)) if len(term) >= 2}


def cluster(rows: Sequence[Dict[str, Any]], min_jaccard: float = MIN_JACCARD,
            min_items: int = MIN_ITEMS, max_groups: int = MAX_GROUPS) -> List[Dict[str, Any]]:
    """按 (uid, scope, group_id, kind) 分组，组内按二元组相似度聚成簇。"""
    buckets: Dict[Any, List[Dict[str, Any]]] = {}
    for row in rows:
        try:
            if str(row.get("kind") or "") not in MERGEABLE_KINDS:
                continue
            if not str(row.get("content") or "").strip():
                continue
        except Exception:  # cleanup: 单条坏数据不影响整体聚类
            continue
        key = (str(row.get("uid") or ""), str(row.get("scope") or "private"),
               str(row.get("group_id") or ""), str(row.get("kind") or ""))
        buckets.setdefault(key, []).append(row)
    groups: List[Dict[str, Any]] = []
    for items in buckets.values():
        for row in items:
            row["_tokens"] = _tokens(row.get("content"))
        used: Set[int] = set()
        for i, j in combinations(range(len(items)), 2):
            if i in used or j in used:
                continue
            left, right = items[i], items[j]
            left_core, right_core = _core(left.get("content")), _core(right.get("content"))
            if _shared_prefix_ratio(left_core, right_core) >= 0.5:
                continue  # 共同开头占一半以上：模板/套话，不是同一件事
            shared = left["_tokens"] & right["_tokens"]
            smaller = min(len(left["_tokens"]), len(right["_tokens"]))
            if not shared or not smaller:
                continue
            # 用「重叠系数」而不是 Jaccard：同一件事换个说法时，短句常是长句的子集
            score = len(shared) / float(smaller)
            if score < min_jaccard or len(shared) < 2:
                continue
            used.update((i, j))
            members = [left, right]
            for k in range(len(items)):
                if k in used:
                    continue
                extra = items[k]
                union_all = set().union(*[m["_tokens"] for m in members])
                smaller = min(len(union_all), len(extra["_tokens"]))
                if not smaller:
                    continue
                overlap = len(union_all & extra["_tokens"]) / float(smaller)
                if overlap >= min_jaccard * 0.8 and len(union_all & extra["_tokens"]) >= 2:
                    used.add(k)
                    members.append(extra)
                    if len(members) >= MAX_ITEMS:
                        break
            keep = max(members, key=lambda r: (float(r.get("importance") or 0), int(r.get("id") or 0)))
            groups.append({
                "ids": [int(m["id"]) for m in members],
                "keep_id": int(keep["id"]),
                "contents": [str(m.get("content") or "") for m in members],
                "shared": sorted(set.intersection(*[m["_tokens"] for m in members])),
                "uid": str(keep.get("uid") or ""),
                "scope": str(keep.get("scope") or "private"),
                "group_id": str(keep.get("group_id") or ""),
                "kind": str(keep.get("kind") or ""),
            })
            if len(groups) >= max_groups:
                return groups
    return groups


def build_prompt(contents: Sequence[str]) -> str:
    rows = "\n".join("%d. %s" % (i + 1, str(text or "").strip()) for i, text in enumerate(contents))
    return (
        "把下面几条关于同一件事的记忆合并成一句事实。要求：\n"
        "1) 保留所有关键信息（谁、什么、时间、偏好、数字），一条都不许丢；\n"
        "2) 不要编造原文没有的信息；\n"
        "3) 只输出合并后的那一句话，不要解释、不要编号。\n\n" + rows
    )


def accept_merged(merged: Any, contents: Sequence[str]) -> bool:
    """防幻觉：长度合理、且覆盖原文至少 50% 的信息词才接受。"""
    text = str(merged or "").strip()
    if not (MIN_CHARS <= len(text) <= MAX_CHARS):
        return False
    union: Set[str] = set()
    for content in contents:
        union |= _tokens(content)
    if not _tokens(text) - union and not union:
        return False
    if not union:
        return len(text) >= MIN_CHARS
    covered = len(_tokens(text) & union) / float(len(union))
    return covered >= 0.5
