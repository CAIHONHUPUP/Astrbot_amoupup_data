# -*- coding: utf-8 -*-
"""services/recall_pipeline.py —— 召回四段里的纯计算（不碰 DB、不读 settings）。

分段（调用顺序与 retrieval_engine 完全一致）：
1. 取候选：clamp_pool / clamp_recent_pool / merge_unique / day_tag —— 只做上界与去重；
2. 打分：score_rows —— BM25 融合、语义闸、重要度、新鲜度，逐条；
3. 多样性：pick_diverse —— 同一件事只留最相关那条（fact_key + jaccard）；
4. 兜底：rank_meta_fallback —— 元问题按「重要度 + 新鲜度」排序。

这些函数原来内联在 retrieval_engine 里，抽出来只为能单测；阈值、权重、运算顺序
一个没改，证据是 tests/contracts/recall_golden.json 的 A/B 逐字节一致。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, cast

try:  # 包模式
    from ..services import fact_dedupe, memory_decay
    from ..utils.text_process import (
        RECALL_W_FRESH, RECALL_W_IMPORTANCE, RECALL_W_REL, _freshness, _jaccard,
    )
except ImportError:  # 顶层导入（测试/脚本）
    from services import fact_dedupe, memory_decay
    from utils.text_process import (
        RECALL_W_FRESH, RECALL_W_IMPORTANCE, RECALL_W_REL, _freshness, _jaccard,
    )

#: 候选池硬上限：库再大，一次召回也只取这么多行（性能护栏）
MAX_CANDIDATE_POOL = 4000
#: 「最近活跃」补充层：默认值 / 上限
DEFAULT_RECENT_POOL = 600
MAX_RECENT_POOL = 4000

#: 「昨天」类问题里算「模糊问法」的措辞（对应的日总结会额外加权）
VAGUE_DAY_WORDS = (
    "干了什么", "干什么", "干啥", "做了什么", "做了啥",
    "聊了什么", "说了什么", "怎么过的", "过得怎么样",
)


# ---------------------------------------------------------------------------
# 1) 取候选：边界与去重
# ---------------------------------------------------------------------------

def clamp_pool(pool_size: int, total: int, limit: int) -> int:
    """自适应候选池：库小就全量精排，库大截断；永远 <= MAX_CANDIDATE_POOL。

    与原实现逐字等价：max(limit, min(max(pool_size, total), 4000))。
    """
    return max(int(limit), min(max(int(pool_size), int(total)), MAX_CANDIDATE_POOL))


def clamp_recent_pool(value: Any, limit: int) -> int:
    """「最近活跃」层的条数：0/None 退回默认 600，上限 4000，且不少于 limit。"""
    raw = int(value or DEFAULT_RECENT_POOL)
    return max(int(limit), min(raw, MAX_RECENT_POOL))


def merge_unique(rows: List[Any], extra: List[Any]) -> List[Any]:
    """按 id 去重后追加（保持 rows 原顺序，extra 里已有的跳过）。"""
    seen = {int(row["id"]) for row in rows}
    return list(rows) + [row for row in extra if int(row["id"]) not in seen]


def day_tag(span: Optional[Dict[str, Any]]) -> str:
    """把时间词解析结果格式化成 YYYY-MM-DD（span 缺字段时抛异常的语义与原实现一致）。"""
    # cast 只影响类型检查：span 为 None 时仍与原实现一样抛 TypeError（调用方已兜住）。
    real_span = cast(Dict[str, Any], span)
    return "%04d-%02d-%02d" % (int(real_span["year"]), int(real_span["month"]), int(real_span["day"]))


def is_date_hit(span: Optional[Dict[str, Any]], content: Any) -> bool:
    """这条日总结是不是就问的那一天。"""
    if not span:
        return False
    try:
        return day_tag(span) in str(content or "")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 2) 打分：逐条
# ---------------------------------------------------------------------------

def keyword_strength(infos, qchars, doc_set, min_terms):
    """关键词证据：(有信息量的命中数, 单字命中数, 是否够格当证据)。"""
    strong_hits = len(infos & doc_set) if infos else 0
    char_hits = len(qchars & doc_set) if qchars else 0
    keyword_ok = (
        strong_hits >= min_terms
        or (len(qchars) == 1 and char_hits >= 1)
        or (len(qchars) == 2 and char_hits >= 2)
    )
    return strong_hits, char_hits, keyword_ok


def score_row(row, index: int, features: Dict[str, Any], ctx: Dict[str, Any],
              top_rel: float, info_count: int) -> Optional[Dict[str, Any]]:
    """给一条候选打分；被两道闸挡掉时返回 None（等价于原实现的 continue）。"""
    text = ctx["text"]
    span = ctx["span"]
    now, half_life_days = ctx["now"], ctx["half_life_days"]
    importance_decay = ctx["importance_decay"]
    docs, raw = features["docs"], features["raw"]
    infos, qchars = features["infos"], features["qchars"]
    q_vec, coss = features["q_vec"], features["coss"]
    vec_pass = features["vec_pass"]
    vec_weight, min_terms = features["vec_weight"], features["min_terms"]

    rel = (raw[index] / top_rel) if top_rel > 0 else 0.0
    doc_set = set(docs[index])
    # 关键词覆盖率：命中越多查询里的词，越可信
    if infos:
        covered = len(infos & doc_set) / float(info_count)
        rel = 0.65 * rel + 0.35 * covered
    cos = coss.get(int(row["id"]), 0.0) if q_vec else 0.0
    date_hit = is_date_hit(span, row["content"]) if span else False
    entity_ok = int(row["id"]) in features.get("entity_pass_ids", set())
    if text and not (rel > 0 or cos > 0 or date_hit) and not entity_ok:
        return None
    strong_hits, char_hits, keyword_ok = keyword_strength(infos, qchars, doc_set, min_terms)
    if q_vec:
        w = vec_weight if not keyword_ok else min(vec_weight, 0.2)
        rel = (1.0 - w) * rel + w * max(0.0, cos)
    if (q_vec and keyword_ok and not date_hit and len(infos) >= 2
            and strong_hits <= 1 and cos < 0.35):
        return None
    vec_ok = bool(vec_pass) or int(row["id"]) in features.get("vec_pass_ids", set())
    if text and not keyword_ok and not date_hit:
        if not (vec_ok or entity_ok):
            return None
    # 新鲜度只看「写下多久了」：以前用 last_hit_at，被召回一次就显新，
    # 和下面的 hit_boost 重复计权，错误记忆反而越召越自信。
    # 半衰期按 kind 分：流水账 7 天、摘要 90 天、事实接近永久。
    fresh = _freshness(row["created_at"], now,
                       memory_decay.half_life_for(row["kind"], half_life_days))
    try:
        imp = max(0.0, min(1.0, float(row["importance"] or 0.5)))
    except (TypeError, ValueError):
        imp = 0.5
    weight = max(0.0, min(0.9, float(importance_decay or 0.0)))
    imp_eff = imp * (1.0 - weight) + imp * weight * fresh
    try:
        hits = int(row["hit_count"] or 0)
    except (TypeError, ValueError):
        hits = 0
    hit_boost = min(1.0, math.log1p(hits) / math.log1p(10)) if hits > 0 else 0.0

    if text:
        score = (
            RECALL_W_REL * rel
            + RECALL_W_IMPORTANCE * imp_eff
            + RECALL_W_FRESH * (0.6 * fresh + 0.4 * hit_boost)
        )
    else:
        denom = RECALL_W_IMPORTANCE + RECALL_W_FRESH
        score = (RECALL_W_IMPORTANCE * imp_eff + RECALL_W_FRESH * (0.6 * fresh + 0.4 * hit_boost)) / denom
    kind = str(row["kind"] or "")
    if kind == "daily_digest" and (not text or rel >= 0.35):
        score += 0.04
    # 「昨天我们干了什么」这类问题：把对应那天的日总结直接顶上来
    if kind == "daily_digest" and span:
        try:
            matched_day = day_tag(span) in str(row["content"] or "")
        except Exception:
            matched_day = False
        if matched_day:
            vague = any(word in text for word in VAGUE_DAY_WORDS)
            score += 0.5 if vague else 0.2
    elif kind == "conversation_summary" and (not text or rel >= 0.35):
        score += 0.02
    item = dict(row)
    item["score"] = round(score, 4)
    item["matched_terms"] = max(strong_hits, char_hits)
    item["vec_score"] = round(cos, 4) if q_vec else 0.0
    return item


def score_rows(rows, features: Dict[str, Any], ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    """打分段：逐条打分后按分数降序（稳定排序，与原实现一致）。"""
    top_rel = max(features["raw"]) if features["raw"] else 0.0
    info_count = len(features["infos"]) or 1
    scored: List[Dict[str, Any]] = []
    for index, row in enumerate(rows):
        item = score_row(row, index, features, ctx, top_rel, info_count)
        if item is not None:
            scored.append(item)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# 3) 多样性 / 4) 元问题兜底
# ---------------------------------------------------------------------------

def pick_diverse(scored, tokens_by_id: Dict[int, set], limit: int,
                 div_ratio: float) -> List[Dict[str, Any]]:
    """同一件事只留最相关那条（事实键相同 或 二元组 Jaccard >= 阈值就跳过）。"""
    items: List[Dict[str, Any]] = []
    selected_tokens: List[set] = []
    selected_facts = set()
    for item in scored:
        if len(items) >= limit:
            break
        toks = tokens_by_id.get(int(item["id"]), set())
        fkey = fact_dedupe.fact_key(str(item.get("content") or ""))
        if fkey and fkey in selected_facts:
            continue
        if div_ratio > 0 and toks and any(
            _jaccard(toks, chosen) >= div_ratio for chosen in selected_tokens
        ):
            continue
        items.append(item)
        selected_tokens.append(toks)
        if fkey:
            selected_facts.add(fkey)
    return items


def rank_meta_fallback(rows, now: int, half_life_days: float,
                       importance_decay: float, limit: int) -> List[Dict[str, Any]]:
    """兜底段：没有关键词可匹配时，按「重要度 + 新鲜度」给几条最值得说的。"""
    picked: List[Dict[str, Any]] = []
    for row in rows:
        try:
            imp = max(0.0, min(1.0, float(row["importance"] or 0.5)))
        except (TypeError, ValueError):
            imp = 0.5
        fresh = _freshness(row["created_at"], now,
                           memory_decay.half_life_for(row["kind"], half_life_days))
        weight = max(0.0, min(0.9, float(importance_decay or 0.0)))
        imp_eff = imp * (1.0 - weight) + imp * weight * fresh
        try:
            hits = int(row["hit_count"] or 0)
        except (TypeError, ValueError):
            hits = 0
        hit_boost = min(1.0, math.log1p(hits) / math.log1p(10)) if hits > 0 else 0.0
        score = 0.65 * imp_eff + 0.35 * (0.6 * fresh + 0.4 * hit_boost)
        item = dict(row)
        item["score"] = round(score, 4)
        item["matched_terms"] = 0
        item["vec_score"] = 0.0
        item["meta_fallback"] = True
        picked.append(item)
        if len(picked) >= limit:
            break
    return picked
