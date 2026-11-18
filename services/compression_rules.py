# -*- coding: utf-8 -*-
"""services/compression_rules.py —— 压缩的纯规则（不碰 DB、不读 settings）。

从 services/compression_engine.py 抽出来的三件事：
* split_long_text：按句子边界把长摘要切成 <= limit 的块（拼回去不丢字）；
* 同天摘要的分组键与合并计划（正文拼接、超预算就停手）；
* 近重复判定：返回该合并的 (保留 id, 被并 id) 对，顺序与原双循环完全一致。
引擎只负责取行、写库；规则全部集中在这里，可以脱离 SQLite 单测。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple

#: 句子边界：中文句号/问号/叹号/分号/换行；用 lookbehind 保留分隔符，拼回去还是原文
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;\n])")
#: 摘要正文里的日期（同一天才合并）
_DAY_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

#: 近重复判定的门槛（与原实现逐字一致）
NEAR_DUPE_MIN_TOKENS = 8
NEAR_DUPE_CONTAINMENT = 0.9
NEAR_DUPE_RATIO = 0.7
NEAR_DUPE_LENGTH_DRIFT = 0.5


def split_long_text(text: Any, limit: int) -> List[str]:
    """按句子边界把长文本切成 <= limit 的块；单句超长时硬切。拼回去不丢字。"""
    body = str(text or "")
    limit = int(limit or 0)
    if limit <= 0 or len(body) <= limit:
        return [body] if body else []
    pieces: List[str] = []
    buf = ""
    for sentence in _SENTENCE_RE.split(body):
        while len(sentence) > limit:
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(sentence[:limit])
            sentence = sentence[limit:]
        if buf and len(buf) + len(sentence) > limit:
            pieces.append(buf)
            buf = ""
        buf += sentence
    if buf:
        pieces.append(buf)
    return [piece for piece in pieces if piece]


def summary_day_key(content: Any) -> str:
    """从摘要正文里取日期键（YYYY-MM-DD）；没有日期返回空串。"""
    match = _DAY_RE.search(str(content or ""))
    return match.group(1) if match else ""


def summary_group_key(uid: Any, scope: Any, group_id: Any, content: Any):
    """同天摘要的分组键：谁 + 哪一天 + 哪个会话（私聊/群聊不能并）。"""
    day = summary_day_key(content)
    if not day:
        return None
    return (str(uid or ""), day, str(scope or "private"), str(group_id or ""))


def plan_summary_merge(items: Sequence[Any], cap: int):
    """同一天摘要的合并计划：(保留行, 合并后正文, 被吸收的行列表)。

    保留最长最新那条；逐条把正文拼上去，超预算就停手（绝不带病标 merged）。
    """
    ordered = sorted(
        items,
        key=lambda r: (len(str(r["content"] or "")), int(r["created_at"] or 0)),
        reverse=True,
    )
    keep = ordered[0]
    merged_text = str(keep["content"] or "")
    absorbed: List[Any] = []
    for row in ordered[1:]:
        piece = str(row["content"] or "").strip()
        if not piece or piece in merged_text:
            absorbed.append(row)  # 正文已经在保留行里，标 merged 不会丢内容
            continue
        candidate = merged_text + "\n" + piece
        if len(candidate) > (cap or 4000):
            break  # 超预算：这条和后面的都保持 active
        merged_text = candidate
        absorbed.append(row)
    return keep, merged_text, absorbed


def near_duplicate_pairs(tokens: Sequence[Tuple], min_jaccard: float) -> List[Tuple[int, int]]:
    """近重复对：(保留 id, 被并 id)。tokens 元素是 (id, kind, scope, group_id, token_list)。

    与原实现的双循环条件、跳过顺序完全一致（同一个 kind、同一个会话、长度接近、
    双向相似且共享词占比够高）。
    """
    sets = [set(toks) for (_id, _kind, _scope, _group, toks) in tokens]
    pairs: List[Tuple[int, int]] = []
    for i in range(len(tokens)):
        id_a, kind_a, scope_a, group_a, tok_a = tokens[i]
        if len(tok_a) < NEAR_DUPE_MIN_TOKENS:
            continue
        for j in range(i + 1, len(tokens)):
            id_b, kind_b, scope_b, group_b, tok_b = tokens[j]
            if kind_a != kind_b:
                continue
            # 跨会话的近重复不合并：群里的那条接不到私聊行上
            if scope_a != scope_b or group_a != group_b:
                continue
            if len(tok_b) < NEAR_DUPE_MIN_TOKENS:
                continue
            longest = max(len(tok_a), len(tok_b))
            if abs(len(tok_a) - len(tok_b)) > longest * NEAR_DUPE_LENGTH_DRIFT:
                continue
            # 双向都要像，且长度接近，避免"信息更少的短句被长句吞掉"
            sa, sb = sets[i], sets[j]
            shared = len(sa & sb)
            jaccard = shared / float(len(sa | sb) or 1)
            containment = shared / float(min(len(sa), len(sb)) or 1)
            ratio = float(min(len(tok_a), len(tok_b))) / float(longest or 1)
            if jaccard < float(min_jaccard) or containment < NEAR_DUPE_CONTAINMENT \
                    or ratio < NEAR_DUPE_RATIO:
                continue
            pairs.append((int(id_a), int(id_b)))
    return pairs
