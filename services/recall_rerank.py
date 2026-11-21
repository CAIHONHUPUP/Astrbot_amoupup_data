# -*- coding: utf-8 -*-
"""services/recall_rerank.py —— 召回二阶段重排（一次便宜的 listwise 选择）。

为什么值得：关键词/向量融合只能保证「像」，不能保证「能回答这个问题」——
误召回多是语义相近但答非所问（同一个人不同时期的事、同一类话题的不同对象）。
只把 top-N 的 id + 40 字摘要交给一次小模型，让它挑出真正相关的几条，
就能在不改召回覆盖面（不会再丢记忆）的前提下把注入精度拉回来。

安全约定：
* 只允许模型「重排 / 丢弃」，不允许新增 id（防幻觉记忆进 prompt）；
* 调用失败、超时、解析不出来、返回空 → 一律原样返回；
* 输入已按分数排好，输出长度不超过 limit。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable, Dict, List, Sequence

#: 每轮最多给模型看几条候选（再多它会开始偷懒）
MAX_CANDIDATES = 20
#: 单条摘要截断（字符）
SNIPPET_CHARS = 40

PROMPT = """你是记忆检索的重排器。下面是候选记忆（id + 摘要）和用户这轮的问题。
只保留真正能回答这个问题的记忆，按相关度从高到低排序；宁缺毋滥，最多 {limit} 条。
不要新增候选，不要解释，只输出 JSON。

输出格式：{{"ids": [1, 2]}}

问题：{query}
候选：
{rows}
"""


def build_prompt(query: str, items: Sequence[Dict[str, Any]], limit: int,
                 snippets: int = SNIPPET_CHARS) -> str:
    rows = "\n".join(
        "- id=%s | %s" % (item.get("id"), str(item.get("content") or "")[:max(8, int(snippets))])
        for item in items
    )
    return PROMPT.format(limit=max(1, int(limit)), query=str(query or "").strip(), rows=rows)


def parse_ids(text: str, valid: Sequence[int], limit: int) -> List[int]:
    """从模型输出里取 id：优先 JSON 的 ids，其次见到的数字；只留合法 id。"""
    allowed = {int(memory_id) for memory_id in valid}
    limit = max(0, int(limit))
    picked: List[int] = []

    def _take(values) -> None:
        for value in values:
            try:
                mid = int(value)
            except (TypeError, ValueError):
                continue
            if mid in allowed and mid not in picked:
                picked.append(mid)
            if len(picked) >= limit:
                return

    body = str(text or "")
    try:
        start, end = body.find("{"), body.rfind("}")
        if start >= 0 and end > start:
            payload = json.loads(body[start:end + 1])
            if isinstance(payload, dict) and isinstance(payload.get("ids"), list):
                _take(payload["ids"])
    except (ValueError, TypeError):  # cleanup: JSON 解析失败就走下面的数字兜底
        pass
    if not picked:
        _take(re.findall(r"-?\d+", body))
    return picked[:limit]


async def rerank(items: Sequence[Dict[str, Any]], query: str, limit: int,
                 call: Callable[[str], Awaitable[str]],
                 max_candidates: int = MAX_CANDIDATES,
                 timeout: float = 8.0) -> List[Dict[str, Any]]:
    """返回重排后的副本；任何异常/空结果都原样返回（宁可不准，不可丢记忆）。"""
    rows = list(items or [])
    if len(rows) <= 1 or limit <= 0:
        return rows
    head = rows[:max(1, min(int(max_candidates), len(rows)))]
    tail = rows[len(head):]
    prompt = build_prompt(query, head, limit)
    try:
        reply = await asyncio.wait_for(call(prompt), timeout=max(0.05, float(timeout)))
    except asyncio.TimeoutError:  # cleanup: 超时就退回原顺序，不阻塞这轮聊天
        return rows
    except Exception:  # cleanup: provider 未配置/网络错误同样静默降级
        return rows
    ids = parse_ids(reply, [int(item["id"]) for item in head], limit)
    if not ids:
        return rows
    by_id = {int(item["id"]): item for item in head}
    ordered = [by_id[mid] for mid in ids if mid in by_id]
    if not ordered:
        return rows
    return ordered + tail
