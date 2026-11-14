# -*- coding: utf-8 -*-
"""services/retrieval_engine.py —— 读记忆：候选粗筛、BM25 + 语义融合、相关性闸、限流截断。

召回原则（与 2.x/3.0 完全一致）：
* 关键词证据优先，语义只在"又像又突出"时单独作证；
* 元问题（你还记得我吗）没有实词，按重要度+新鲜度兜底 2~3 条；
* 每次最多返回 limit 条（默认 6，调用方注入上限 15~20 由 amo 侧再截）。
"""

from __future__ import annotations

import math
import sqlite3
from typing import Any, Dict, List, Optional

try:
    from ..utils.text_process import (
        RECALL_HALF_LIFE_DAYS, RECALL_IMPORTANCE_DECAY, RECALL_POOL,
        RECALL_W_FRESH, RECALL_W_IMPORTANCE, RECALL_W_REL,
        _bm25_scores, _clean, _cosine, _freshness, _informative_terms, _jaccard,
        _now, _parse_time_span, _query_chars, _recall_tokens, logger,
    )
except ImportError:  # 顶层导入（测试/脚本）
    from utils.text_process import (
        RECALL_HALF_LIFE_DAYS, RECALL_IMPORTANCE_DECAY, RECALL_POOL,
        RECALL_W_FRESH, RECALL_W_IMPORTANCE, RECALL_W_REL,
        _bm25_scores, _clean, _cosine, _freshness, _informative_terms, _jaccard,
        _now, _parse_time_span, _query_chars, _recall_tokens, logger,
    )

# 「你还记得我吗」这类没有实词的元问题：关键词闸门会一条都不给，单独兜底。
_META_PATTERNS = (
    "记得我吗", "记得我么", "还记得我吗", "记得我是谁", "记得我是谁吗",
    "记得什么", "记得啥", "记得哪些", "还记得什么", "都记得什么",
    "知道我是谁", "了解我吗", "了解我多少",
    "记得我说过什么", "我说过什么", "还记得我说过", "记得我的事",
    "记得我们", "还记得我们",
)


def _is_meta_query(text: Any) -> bool:
    body = "".join(str(text or "").split())
    if not body:
        return False
    return any(word in body for word in _META_PATTERNS)


class RetrievalEngine:
    def __init__(self, db, settings, persons=None, memory=None):
        self.db = db
        self.settings = settings
        self.persons = persons
        self.memory = memory

    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    @property
    def conn(self) -> sqlite3.Connection:
        return self.db.conn

    @property
    def lock(self):
        return self.db.lock

    def _substring_candidates(self, base, params, text, limit):
        """trigram FTS 子串粗筛；不可用/查询太短时返回 None（调用方退回 LIKE）。"""
        if not getattr(self.db, "fts_trigram", False) or len(text) < 3:
            return None
        phrase = '"' + str(text).replace('"', '""') + '"'
        try:
            with self.lock:
                hits = self.conn.execute(
                    "SELECT rowid FROM memories_ftst WHERE memories_ftst MATCH ? LIMIT ?",
                    (phrase, int(limit) * 2),
                ).fetchall()
                if not hits:
                    return None
                ids = [int(row["rowid"]) for row in hits]
                marks = ",".join("?" for _ in ids)
                return self.conn.execute(
                    base + " AND id IN (%s) ORDER BY last_hit_at DESC, created_at DESC LIMIT ?" % marks,
                    list(params) + ids + [int(limit)],
                ).fetchall()
        except Exception as exc:
            logger.debug("[amo_data] trigram 粗筛失败，退回 LIKE: %s" % exc)
            self.db.fts_trigram = False
            return None

    def recall(self, bot_id: str, uid: str, query: str = "", limit: int = 6,
               scope: str = "", group_id: str = "", platform: str = "",
               pid: int = 0, half_life_days: Optional[float] = None,
               importance_decay: Optional[float] = None,
               pool_size: Optional[int] = None) -> List[Dict[str, Any]]:
        """按「相关性 + 重要度 + 新鲜度」综合打分召回。

        相关性 = BM25（单字+二元组）。配了 embedding 时再叠一层语义余弦。
        两道闸防止"什么都推一条"：关键词证据 + 语义证据（余弦>=阈值且领先）。
        """
        bot_id, uid = str(bot_id), str(uid)
        try:
            if not pid:
                if platform:
                    pid = self.persons.person_pid(bot_id, platform, uid)
                else:
                    pid = self.persons.pid_for_uid(bot_id, uid)
            uid = self.persons.canonical_uid_for_uid(bot_id, uid) or uid
        except Exception:
            pid = 0
        pid = int(pid or 0)
        limit = max(1, min(int(limit or 6), 100))
        text = _clean(query, 200)
        meta_query = bool(text) and bool(self.setting("meta_query_fallback", True)) \
            and _is_meta_query(text)
        now = _now()
        half_life_days = float(half_life_days if half_life_days is not None
                               else self.setting("forget_half_life_days", RECALL_HALF_LIFE_DAYS) or RECALL_HALF_LIFE_DAYS)
        importance_decay = float(importance_decay if importance_decay is not None
                                 else self.setting("importance_decay", RECALL_IMPORTANCE_DECAY) or 0.0)
        pool_size = int(pool_size if pool_size is not None
                        else self.setting("pool_size", RECALL_POOL) or RECALL_POOL)

        # 解析查询里的时间词（昨天/前天/11月6号…），给对应日期的阶段记忆加权
        span = None
        if _parse_time_span is not None:
            try:
                span = _parse_time_span(text)
            except Exception:
                span = None

        # 自适应候选池：库小就全量精排，库大才截断
        try:
            total = self.db.dao_memory_count(bot_id, uid, pid) if hasattr(self.db, "dao_memory_count") else None
        except Exception:
            total = None
        if total is None:
            total = self._count_active(bot_id, uid, pid)
        pool_size = max(limit, min(max(pool_size, total), 4000))

        base = (
            "SELECT id, content, kind, subject, importance, created_at, last_hit_at, hit_count, "
            "scope, group_id, pid, platform "
            "FROM memories WHERE bot_id=? AND COALESCE(status,'active')='active'"
        )
        params: List[Any] = [bot_id]
        if pid:
            # 按人物取：新记忆靠 pid，老记忆（pid=0）靠 uid 兜底
            base += " AND (pid=? OR (COALESCE(pid,0)=0 AND uid=?))"
            params.extend([int(pid), uid])
        else:
            base += " AND uid=?"
            params.append(uid)
        if scope in ("private", "group"):
            base += " AND scope=?"
            params.append(scope)
        if group_id:
            base += " AND group_id=?"
            params.append(str(group_id))

        rows: List[sqlite3.Row] = []
        # 粗筛 1：子串命中（>=3 字走 trigram，否则 LIKE）
        if text:
            rows = self._substring_candidates(base, params, text, pool_size) or []
            if not rows:
                with self.lock:
                    rows = self.conn.execute(
                        base + " AND content LIKE ? ORDER BY last_hit_at DESC, created_at DESC LIMIT ?",
                        params + ["%" + text + "%", pool_size],
                    ).fetchall()
        # 粗筛 2：补最近活跃的，保证词序不同的记忆也有候选
        recent_pool = max(limit, min(int(self.setting("recall_recent_pool", 600) or 600), 4000))
        with self.lock:
            seen = {int(r["id"]) for r in rows}
            more = self.conn.execute(
                base + " ORDER BY last_hit_at DESC, created_at DESC LIMIT ?",
                params + [recent_pool],
            ).fetchall()
        rows.extend([r for r in more if int(r["id"]) not in seen])

        # 问"昨天/前天"时，把对应当天的日总结直接拉进候选池
        if span:
            try:
                date_tag = "%04d-%02d-%02d" % (int(span["year"]), int(span["month"]), int(span["day"]))
                with self.lock:
                    digests = self.conn.execute(
                        base + " AND kind='daily_digest' AND content LIKE ? LIMIT 3",
                        params + ["%" + date_tag + "%"],
                    ).fetchall()
                seen = {int(r["id"]) for r in rows}
                rows.extend([r for r in digests if int(r["id"]) not in seen])
            except Exception:
                pass
        if not rows:
            # 元问题没有实词，关键词粗筛一条都进不来
            return self._meta_fallback(bot_id, uid, scope, group_id, now,
                                       half_life_days, importance_decay) if meta_query else []

        contents = [str(r["content"] or "") for r in rows]
        q_tokens = _recall_tokens(text) if text else []
        docs = [_recall_tokens(c) for c in contents]
        raw = _bm25_scores(q_tokens, docs) if q_tokens else [0.0 for _ in rows]

        # 可选语义分：查询向量 + 候选向量（超时/没装 Ollama 时自然退化成纯 BM25）
        q_vec: List[float] = []
        vec_map: Dict[int, List[float]] = {}
        if text and self.setting("embedding_enabled"):
            q_vec = self.memory._embed_cached(text)
            if q_vec:
                vec_map = self.memory._load_vecs([int(r["id"]) for r in rows])
        vec_weight = max(0.0, min(0.9, float(self.setting("vec_weight", 0.45) or 0.0)))
        vec_min_cos = float(self.setting("vec_min_cos", 0.45) or 0.0)
        vec_min_margin = float(self.setting("vec_min_margin", 0.08) or 0.0)
        min_terms = int(self.setting("min_matched_terms", 1) or 0)
        infos = set(_informative_terms(text)) if text else set()
        qchars = set(_query_chars(text)) if text else set()

        # 语义闸：只有"明显比其它候选更像"的那条才允许在没关键词的情况下入选。
        coss: Dict[int, float] = {}
        best_cos = 0.0
        vec_pass = False
        if q_vec:
            for row in rows:
                coss[int(row["id"])] = _cosine(q_vec, vec_map.get(int(row["id"]), []))
            ranked = sorted(coss.values(), reverse=True)
            best_cos = ranked[0] if ranked else 0.0
            second = ranked[1] if len(ranked) > 1 else 0.0
            margin = best_cos - second
            # bge-m3 中文余弦偏平（无关也能到 0.7+），门槛 0.82 且 top 要领先
            vec_pass = best_cos >= max(vec_min_cos, 0.82) and margin >= vec_min_margin

        scored: List[Dict[str, Any]] = []
        top_rel = max(raw) if raw else 0.0
        info_count = len(infos) or 1
        for idx, row in enumerate(rows):
            rel = (raw[idx] / top_rel) if top_rel > 0 else 0.0
            doc_set = set(docs[idx])
            # 关键词覆盖率：命中越多查询里的词，越可信
            if infos:
                covered = len(infos & doc_set) / float(info_count)
                rel = 0.65 * rel + 0.35 * covered
            cos = coss.get(int(row["id"]), 0.0) if q_vec else 0.0
            date_hit = False
            if span and str(row["kind"] or "") == "daily_digest":
                try:
                    date_hit = ("%04d-%02d-%02d" % (int(span["year"]), int(span["month"]), int(span["day"]))
                                in str(row["content"] or ""))
                except Exception:
                    date_hit = False
            if text and not (rel > 0 or cos > 0 or date_hit):
                continue
            strong_hits = len(infos & doc_set) if infos else 0
            char_hits = len(qchars & doc_set) if qchars else 0
            keyword_ok = (
                strong_hits >= min_terms
                or (len(qchars) == 1 and char_hits >= 1)
                or (len(qchars) == 2 and char_hits >= 2)
            )
            if q_vec:
                w = vec_weight if not keyword_ok else min(vec_weight, 0.2)
                rel = (1.0 - w) * rel + w * max(0.0, cos)
            if (q_vec and keyword_ok and not date_hit and len(infos) >= 2
                    and strong_hits <= 1 and cos < 0.35):
                continue
            if text and not keyword_ok and not date_hit:
                if not vec_pass or cos < best_cos - 1e-9:
                    continue
            fresh = _freshness(row["last_hit_at"] or row["created_at"], now, half_life_days)
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
            # "昨天我们干了什么"这类问题：把对应那天的日总结直接顶上来
            if kind == "daily_digest" and span:
                try:
                    date_tag = "%04d-%02d-%02d" % (int(span["year"]), int(span["month"]), int(span["day"]))
                    if date_tag in str(row["content"] or ""):
                        vague = any(word in text for word in
                                    ("干了什么", "干什么", "干啥", "做了什么", "做了啥",
                                     "聊了什么", "说了什么", "怎么过的", "过得怎么样"))
                        score += 0.5 if vague else 0.2
                except Exception:
                    pass
            elif kind == "conversation_summary" and (not text or rel >= 0.35):
                score += 0.02
            item = dict(row)
            item["score"] = round(score, 4)
            item["matched_terms"] = max(strong_hits, char_hits)
            item["vec_score"] = round(cos, 4) if q_vec else 0.0
            scored.append(item)

        scored.sort(key=lambda x: x["score"], reverse=True)
        # 注入去重/多样化：同一件事只留最相关的一条
        tokens_by_id = {int(row["id"]): set(docs[idx]) for idx, row in enumerate(rows)}
        div_ratio = float(self.setting("diversify_ratio", 0.75) or 0.0)
        items: List[Dict[str, Any]] = []
        selected_tokens: List[set] = []
        selected_facts = set()
        for item in scored:
            if len(items) >= limit:
                break
            toks = tokens_by_id.get(int(item["id"]), set())
            try:
                from services import fact_dedupe as _facts
            except ImportError:  # pragma: no cover
                _facts = None
            fkey = _facts.fact_key(str(item.get("content") or "")) if _facts else ""
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

        if not items and meta_query:
            items = self._meta_fallback(bot_id, uid, scope, group_id, now,
                                        half_life_days, importance_decay)
        if items:
            with self.lock:
                self.conn.executemany(
                    "UPDATE memories SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
                    [(now, item["id"]) for item in items],
                )
                self.conn.commit()
        if self.setting("identity_debug", True):
            logger.info(
                "[MEMORY_DEBUG] person_id=%s source_platform=%s hit=%s reason=recall:%s",
                pid, platform or "unknown", len(items), "meta" if meta_query else "query",
            )
        return items

    def _count_active(self, bot_id: str, uid: str, pid: int) -> int:
        try:
            with self.lock:
                if pid:
                    row = self.conn.execute(
                        "SELECT COUNT(*) AS n FROM memories WHERE bot_id=? AND (pid=? OR "
                        "(COALESCE(pid,0)=0 AND uid=?)) AND COALESCE(status,'active')='active'",
                        (bot_id, int(pid), uid),
                    ).fetchone()
                else:
                    row = self.conn.execute(
                        "SELECT COUNT(*) AS n FROM memories WHERE bot_id=? AND uid=? "
                        "AND COALESCE(status,'active')='active'",
                        (bot_id, uid),
                    ).fetchone()
            return int(row["n"]) if row else 0
        except Exception:
            return 0

    def _meta_fallback(self, bot_id: str, uid: str, scope: str, group_id: str,
                       now: int, half_life_days: float,
                       importance_decay: float) -> List[Dict[str, Any]]:
        """元问题兜底：没有关键词可匹配时，按「重要度 + 新鲜度」给几条最值得说的。"""
        if not self.setting("meta_query_fallback", True):
            return []
        limit = max(1, min(int(self.setting("meta_query_limit", 3) or 3), 10))
        pid = self.persons.pid_for_uid(bot_id, uid) if self.persons else 0
        if pid:
            where = "bot_id=? AND (pid=? OR (COALESCE(pid,0)=0 AND uid=?)) AND COALESCE(status,'active')='active'"
            params: List[Any] = [str(bot_id), int(pid), str(uid)]
        else:
            where = "bot_id=? AND uid=? AND COALESCE(status,'active')='active'"
            params = [str(bot_id), str(uid)]
        if scope in ("private", "group"):
            where += " AND scope=?"
            params.append(scope)
        if group_id:
            where += " AND group_id=?"
            params.append(str(group_id))
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, content, kind, subject, importance, created_at, last_hit_at, hit_count, scope, group_id "
                "FROM memories WHERE " + where +
                " ORDER BY importance DESC, last_hit_at DESC LIMIT ?",
                params + [limit * 4],
            ).fetchall()
        picked: List[Dict[str, Any]] = []
        for row in rows:
            try:
                imp = max(0.0, min(1.0, float(row["importance"] or 0.5)))
            except (TypeError, ValueError):
                imp = 0.5
            fresh = _freshness(row["last_hit_at"] or row["created_at"], now, half_life_days)
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
        if picked:
            with self.lock:
                self.conn.executemany(
                    "UPDATE memories SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
                    [(now, item["id"]) for item in picked],
                )
                self.conn.commit()
        return picked
