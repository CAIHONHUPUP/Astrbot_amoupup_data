# -*- coding: utf-8 -*-
"""services/entity_tag.py —— 写入时给记忆挂人物实体标签（实体路按名字召回）。

实体路原来只认「查询里有名字/关系词 → 按 pid 取这个人的记忆」。可很多回忆写的是
「他今天又加班」，正文里没有名字、pid 也为 0（老数据/群聊/导入），这一路就漏了。

这里在写入时做一次纯规则的实体标注：正文里出现的已知人物名字 → 追加
`entity:<名字>` 标签（和 `#活动 event:<id>` 同一套空格分隔格式）。
召回时 graph_recall 先按名字取 tag 命中的记忆，再按 pid 取，
两条路都有的记忆自然合并去重。

不调模型、不联网；名字表由 person_engine 的 persons/person_links 提供。
"""
from __future__ import annotations

from typing import Iterable, List, Sequence

#: 一条记忆最多挂几个实体（多了会把标签变成噪声）
MAX_ENTITY_TAGS = 3


def match_names(text: str, names: Sequence[str]) -> List[str]:
    """正文里出现的名字（>=2 字、按长度降序避免「小周」被「小周周」抢先）。"""
    body = str(text or "")
    if not body:
        return []
    hits = []
    for name in {str(n or "").strip() for n in names}:
        if len(name) < 2:
            continue
        pos = body.find(name)
        if pos >= 0:
            hits.append((pos, -len(name), name))
    hits.sort()
    return [name for _pos, _neg, name in hits[:MAX_ENTITY_TAGS]]


def entity_tokens(names: Iterable[str]) -> List[str]:
    return ["entity:" + str(name).strip() for name in names if str(name or "").strip()]


def apply_entity_tags(text: str, tags: str, names: Sequence[str]) -> str:
    """把 `entity:<名字>` 并进 tags（已有不重复，保持原顺序）。"""
    existing = [token for token in str(tags or "").split() if token]
    for token in entity_tokens(match_names(text, names)):
        if token not in existing:
            existing.append(token)
    return " ".join(existing)
