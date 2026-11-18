# -*- coding: utf-8 -*-
"""services/retrieval_engine.py —— 读记忆：候选粗筛、BM25 + 语义融合、相关性闸、限流截断。

召回原则（与 2.x/3.0 完全一致）：
* 关键词证据优先，语义只在"又像又突出"时单独作证；
* 元问题（你还记得我吗）没有实词，按重要度+新鲜度兜底 2~3 条；
* 每次最多返回 limit 条（默认 6，调用方注入上限 15~20 由 amo 侧再截）。

Phase 13 P2 起，召回固定拆成四段（顺序与权重一个没改，A/B 见
tests/contracts/recall_golden.json）：
    1. 取候选  _recall_candidates（SQL 粗筛 + 最近活跃 + 时间词日总结）
    2. 打分    _recall_score（BM25/语义融合 + 两道闸，纯计算在 recall_pipeline）
    3. 多样性  _recall_diversify（事实键 + jaccard，纯计算在 recall_pipeline）
    4. 兜底    _meta_fallback（元问题按重要度+新鲜度）
本文件只留 SQL、设置读取和命中写回；纯函数在 services/recall_pipeline.py。
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Dict, List, Optional

try:
    from . import chain as _chain
except ImportError:  # 顶层导入（测试/脚本）
    from services import chain as _chain

try:
    from ..utils.text_process import (
        RECALL_HALF_LIFE_DAYS, RECALL_IMPORTANCE_DECAY, RECALL_POOL,
        _bm25_scores, _clean, _cosine, _informative_terms, _jaccard,
        _now, _parse_time_span, _query_chars, _recall_tokens, logger,
    )
except ImportError:  # 顶层导入（测试/脚本）
    from utils.text_process import (
        RECALL_HALF_LIFE_DAYS, RECALL_IMPORTANCE_DECAY, RECALL_POOL,
        _bm25_scores, _clean, _cosine, _informative_terms, _jaccard,
        _now, _parse_time_span, _query_chars, _recall_tokens, logger,
    )

# fact_dedupe 必须相对导入优先：AstrBot 以 data.plugins.<插件> 包方式加载时没有顶层
# services 模块，函数里临时 `from services import ...` 会 ImportError 被吞掉，
# 注入去重就静默失效（同一件事会在召回里出现两条）。
try:
    from ..services import entity_graph, graph_recall, query_rewrite, recall_index, recall_pipeline
except ImportError:  # 顶层导入（测试/脚本）
    from services import entity_graph, graph_recall, query_rewrite, recall_index, recall_pipeline

# 兼容：注入去重规则现在在 recall_pipeline，但老代码/测试会从本模块取 fact_dedupe
fact_dedupe = recall_pipeline.fact_dedupe


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
        # 全库向量候选层：memory_vec 只在条数/最大 id/更新时间变化时重建
        self._vec_index = recall_index.VectorIndex(self._vec_loader)
        self._index_lock = threading.Lock()

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
               pool_size: Optional[int] = None,
               tags: str = "") -> List[Dict[str, Any]]:
        """按「相关性 + 重要度 + 新鲜度」综合打分召回。

        相关性 = BM25（单字+二元组）。配了 embedding 时再叠一层语义余弦。
        两道闸防止"什么都推一条"：关键词证据 + 语义证据（余弦>=阈值且领先）。
        tags 非空时只在带这个标签的记忆里召回（"#活动 event:<id>" 这类，整词匹配）：
        query 可以为空，此时按「最近活跃」返回这批带标签的记忆。
        """
        trace = _chain.new_chain("recall", bool(self.setting("chain_trace", True)))
        ctx = self._recall_prepare(bot_id, uid, query, limit, scope, group_id, platform, pid,
                                   half_life_days, importance_decay, pool_size, tags)
        ctx["chain"] = trace
        rows = self._recall_candidates(ctx)
        trace.step("candidates", "n=%d" % len(rows))
        if not rows:
            # 标签过滤下没有候选就是没有：别让元问题兜底绕过过滤条件
            if ctx["tags"]:
                trace.done("empty:tags")
                return []
            # 元问题没有实词，关键词粗筛一条都进不来
            items = self._meta_fallback(ctx["bot_id"], ctx["uid"], ctx["scope"], ctx["group_id"],
                                        ctx["now"], ctx["half_life_days"],
                                        ctx["importance_decay"]) if ctx["meta_query"] else []
            trace.done("empty:meta=%d" % len(items))
            return items

        sp = self._recall_scores(ctx, rows)
        scored = self._recall_score(ctx, rows, sp)
        trace.step("score", "n=%d" % len(scored))
        items = self._recall_diversify(ctx, scored, rows, sp["docs"])

        if not items and ctx["meta_query"] and not ctx["tags"]:
            items = self._meta_fallback(ctx["bot_id"], ctx["uid"], ctx["scope"], ctx["group_id"],
                                        ctx["now"], ctx["half_life_days"],
                                        ctx["importance_decay"])
        if items:
            with self.lock:
                self.conn.executemany(
                    "UPDATE memories SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
                    [(ctx["now"], item["id"]) for item in items],
                )
                self.conn.commit()
        trace.done("n=%d" % len(items))
        if self.setting("identity_debug", True):
            logger.info(
                "[MEMORY_DEBUG] person_id=%s source_platform=%s hit=%s reason=recall:%s",
                ctx["pid"], ctx["platform"] or "unknown", len(items),
                "meta" if ctx["meta_query"] else "query",
            )
        return items

    def _recall_prepare(self, bot_id, uid, query, limit, scope, group_id, platform, pid,
                         half_life_days, importance_decay, pool_size, tags=""):
        """召回第 0 段：解析身份/范围/时间词/候选池 SQL。"""
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
        pool_size = recall_pipeline.clamp_pool(pool_size, total, limit)

        tag_text = _clean(tags, 200)
        base = (
            "SELECT id, content, kind, subject, importance, created_at, last_hit_at, hit_count, "
            "scope, group_id, pid, platform, COALESCE(tags,'') AS tags "
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
        if tag_text:
            # 标签按「整词」匹配：tags 是空格分隔的（#活动 event:<id>），
            # 用两侧补空格的 LIKE 兜住，免得 event:1 把 event:10 也捞出来。
            for token in tag_text.split():
                base += " AND (' ' || COALESCE(tags,'') || ' ') LIKE ?"
                params.append("% " + token + " %")
        return {
            "bot_id": bot_id, "uid": uid, "pid": pid, "limit": limit, "text": text,
            "meta_query": meta_query, "now": now, "half_life_days": half_life_days,
            "importance_decay": importance_decay, "pool_size": pool_size, "span": span,
            "scope": scope, "group_id": group_id, "platform": platform,
            "tags": tag_text, "base": base, "params": params,
        }

    def _recall_candidates(self, ctx):
        """召回第 1 段（取候选）：子串粗筛 + 最近活跃补充 + 时间词对应日总结。

        空 query 只走「最近活跃」一层（不做 LIKE/BM25），这就是空查询的短路；
        两条 SQL 都用 ? 占位，text 只作为参数传入，不做字符串拼接。
        """
        base, params = ctx["base"], ctx["params"]
        text, pool_size = ctx["text"], ctx["pool_size"]
        limit, span = ctx["limit"], ctx["span"]
        rows: List[sqlite3.Row] = []
        trace = ctx.get("chain")
        # 粗筛 1：子串命中（>=3 字走 trigram，否则 LIKE）；空 query 直接跳过
        if text:
            rows = self._substring_candidates(base, params, text, pool_size) or []
            if not rows:
                if self.setting("recall_token_or", True):
                    rows = self._token_candidates(base, params, text, pool_size)
                if not rows:
                    with self.lock:
                        rows = self.conn.execute(
                            base + " AND content LIKE ? ORDER BY created_at DESC LIMIT ?",
                            params + ["%" + text + "%", pool_size],
                        ).fetchall()
            # 多查询：把剥壳后的核心、拆词变体各粗筛一次，合并候选
            # 多查询走的是同一套拆词粗筛：关掉 token_or 时它也一起关，
            # 这样「关规则必红」的反向验证才成立。
            if self.setting("recall_multi_query", True) and self.setting("recall_token_or", True):
                for variant in query_rewrite.variants(
                        text, int(self.setting("recall_multi_query_variants", 3) or 3))[1:]:
                    if variant and variant != text:
                        rows = recall_pipeline.merge_unique(
                            rows, self._token_candidates(base, params, variant, pool_size))
        # 粗筛 2：补最近活跃的，保证词序不同的记忆也有候选
        recent_pool = recall_pipeline.clamp_recent_pool(
            self.setting("recall_recent_pool", recall_pipeline.DEFAULT_RECENT_POOL), limit
        )
        with self.lock:
            more = self.conn.execute(
                base + " ORDER BY last_hit_at DESC, created_at DESC LIMIT ?",
                params + [recent_pool],
            ).fetchall()
        rows = recall_pipeline.merge_unique(rows, more)
        if trace is not None:
            trace.step("recent", "n=%d" % len(rows))

        # 问"昨天/前天"时，把对应当天的日总结直接拉进候选池
        if span:
            try:
                date_tag = recall_pipeline.day_tag(span)
                with self.lock:
                    digests = self.conn.execute(
                        base + " AND kind='daily_digest' AND content LIKE ? LIMIT 3",
                        params + ["%" + date_tag + "%"],
                    ).fetchall()
                rows = recall_pipeline.merge_unique(rows, digests)
            except Exception as exc:
                logger.debug("[amo_data] optional step failed: %r", exc)
        rows = self._merge_dormant(ctx, rows)
        if trace is not None:
            trace.step("dormant", "n=%d" % len(rows))
        rows = self._merge_vector_candidates(ctx, rows)
        if trace is not None:
            trace.step("vector", "n=%d" % len(rows))
        rows = self._merge_entity_candidates(ctx, rows)
        if trace is not None:
            trace.step("entity", "n=%d" % len(rows))
        return rows

    def _token_candidates(self, base, params, text, pool_size):
        """拆词 OR 粗筛：整句 LIKE 对自然语言几乎无效，「猫 团子」这种跨词序的能捞回来。"""
        terms: List[str] = []
        for term in _informative_terms(text):
            if term and term not in terms:
                terms.append(term)
            if len(terms) >= 6:
                break
        if not terms:
            return []
        clause = " OR ".join("content LIKE ?" for _ in terms)
        try:
            with self.lock:
                return self.conn.execute(
                    base + " AND (" + clause + ") ORDER BY created_at DESC LIMIT ?",
                    list(params) + ["%" + t + "%" for t in terms] + [int(pool_size)],
                ).fetchall()
        except Exception as exc:
            logger.debug("[amo_data] 拆词粗筛失败: %r", exc)
            return []

    def _vec_loader(self):
        """VectorIndex 的回调：拿一次指纹 + 全量向量（调用方持 _index_lock）。

        向量读写都在 MemoryEngine.dao 上（MemoryDB 不代理 DAO），没有 memory
        引擎时（单测的假 DB）就返回空索引，召回退回关键词两路。
        """
        memory = self.memory
        dao = getattr(memory, "dao", None) if memory is not None else None
        if dao is None:
            return (0, 0, 0), {}
        try:
            return dao.vec_state(), dao.load_all_vecs()
        except Exception as exc:
            logger.debug("[amo_data] 向量索引读取失败: %r", exc)
            return (0, 0, 0), {}

    def _query_vec(self, ctx):
        """查询向量：同一次召回里只算一遍（候选层和打分层共用）。"""
        q_vec = ctx.get("q_vec") or []
        if q_vec:
            return q_vec
        if not ctx["text"] or not self.setting("embedding_enabled"):
            return []
        q_vec = self.memory._embed_cached(ctx["text"]) if self.memory else []
        ctx["q_vec"] = q_vec
        return q_vec

    def _merge_vector_candidates(self, ctx, rows):
        """第三路候选：全库向量 kNN。修复「词面不重合就永远进不了池」。"""
        want = int(self.setting("recall_vector_candidates", 50) or 0)
        if want <= 0 or not ctx["text"]:
            return rows
        q_vec = self._query_vec(ctx)
        if not q_vec:
            return rows
        try:
            with self._index_lock:
                hits = self._vec_index.search(q_vec, want)
        except Exception as exc:
            logger.debug("[amo_data] 向量候选失败: %r", exc)
            return rows
        ids = [int(mid) for mid, _ in hits if mid]
        if not ids:
            return rows
        ctx["vec_rank"] = {mid: rank + 1 for rank, mid in enumerate(ids)}
        marks = ",".join("?" for _ in ids)
        try:
            with self.lock:
                extra = self.conn.execute(
                    ctx["base"] + " AND id IN (%s)" % marks,
                    list(ctx["params"]) + ids,
                ).fetchall()
        except Exception as exc:
            logger.debug("[amo_data] 向量候选回表失败: %r", exc)
            return rows
        return recall_pipeline.merge_unique(rows, extra)

    def _merge_dormant(self, ctx, rows):
        """休眠记忆复活：强关键词命中时把它捞回来并标回 active（不是删掉）。"""
        if not self.setting("recall_revive_dormant", True) or not ctx["text"]:
            return rows
        terms = [term for term in _informative_terms(ctx["text"]) if len(term) >= 2][:4]
        if not terms:
            return rows
        base = ctx["base"].replace(
            "COALESCE(status,'active')='active'", "COALESCE(status,'active')='dormant'"
        )
        clause = " OR ".join("content LIKE ?" for _ in terms)
        try:
            with self.lock:
                extra = self.conn.execute(
                    base + " AND (" + clause + ") ORDER BY created_at DESC LIMIT 5",
                    list(ctx["params"]) + ["%" + term + "%" for term in terms],
                ).fetchall()
        except Exception as exc:
            logger.debug("[amo_data] 休眠记忆检索失败: %r", exc)
            return rows
        if not extra:
            return rows
        ids = [int(row["id"]) for row in extra]
        try:
            self.memory.dao.revive_many(ids) if self.memory is not None else None
        except Exception as exc:
            logger.debug("[amo_data] 复活休眠记忆失败: %r", exc)
        ctx.setdefault("entity_ids", set()).update(ids)
        return recall_pipeline.merge_unique(rows, extra)

    def _merge_entity_candidates(self, ctx, rows):
        """实体/关系路：名字、别名、关系词命中人物时，按 pid 把那个人的记忆拉进来。"""
        if not ctx["text"]:
            return rows
        if not self.setting("recall_graph_route", True):
            # 人物路关掉时通用实体图谱仍要跑（两个开关互不牵连）
            return self._merge_graph_memories(ctx, rows)
        limit = int(self.setting("recall_graph_candidates", 10) or 0)
        if limit <= 0:
            return rows
        try:
            found = graph_recall.resolve_entities(
                self.conn, self.lock, ctx["bot_id"], ctx["uid"], ctx["text"])
        except Exception as exc:
            logger.debug("[amo_data] 实体识别失败: %r", exc)
            return rows
        pids = found.get("pids") or []
        names = found.get("names") or []
        if not pids and not names:
            # 没有人物命中也要走通用实体图谱（「围巾→小周→另一条记忆」这类）
            return self._merge_graph_memories(ctx, rows)
        ctx["graph_entities"] = found.get("matched") or []
        extra = graph_recall.fetch_person_memories(
            self.conn, self.lock, ctx["bot_id"], pids, ctx["scope"], ctx["group_id"], limit,
            names=names)
        if extra:
            # 这一路的行没有字面证据，打分层要单独放行（和语义闸同理）
            ctx.setdefault("entity_ids", set()).update(int(row["id"]) for row in extra)
        merged = recall_pipeline.merge_unique(rows, extra)
        return self._merge_graph_memories(ctx, merged)

    def _merge_graph_memories(self, ctx, rows):
        """通用实体图谱：查询实体 → 直接命中 + 一跳共现 → 相关记忆。"""
        if not self.setting("recall_entity_graph", True):
            return rows
        limit = int(self.setting("recall_entity_graph_memories", 10) or 0)
        if limit <= 0:
            return rows
        try:
            ids = entity_graph.memories_for_query(
                self.conn, self.lock, ctx["bot_id"], ctx["text"], limit)
        except Exception as exc:
            logger.debug("[amo_data] 实体图谱检索失败: %r", exc)
            return rows
        if not ids:
            return rows
        marks = ",".join("?" for _ in ids)
        try:
            with self.lock:
                extra = self.conn.execute(
                    ctx["base"] + " AND id IN (%s)" % marks,
                    list(ctx["params"]) + ids,
                ).fetchall()
        except Exception as exc:
            logger.debug("[amo_data] 实体图谱回表失败: %r", exc)
            return rows
        if extra:
            ctx.setdefault("entity_ids", set()).update(int(row["id"]) for row in extra)
        return recall_pipeline.merge_unique(rows, extra)

    def _vec_cosines(self, q_vec, ids):
        """候选池余弦：索引可用就走矩阵点积，否则退回逐条 _cosine。"""
        if not q_vec or not ids:
            return {}
        try:
            with self._index_lock:
                found = self._vec_index.cosines(q_vec, ids)
            if found:
                return {int(k): float(v) for k, v in found.items()}
        except Exception:
            pass
        vec_map = self.memory._load_vecs([int(i) for i in ids]) if self.memory else {}
        return {int(i): _cosine(q_vec, vec_map.get(int(i), [])) for i in ids}

    def _recall_scores(self, ctx, rows):
        """召回第 2 段的前半：BM25 / 向量 / 语义闸这些打基础分要用的量（仍近纯：只读 DB 向量）。"""
        text = ctx["text"]
        contents = [str(r["content"] or "") for r in rows]
        q_tokens = _recall_tokens(text) if text else []
        docs = [_recall_tokens(c) for c in contents]
        raw = _bm25_scores(q_tokens, docs) if q_tokens else [0.0 for _ in rows]

        # 可选语义分：查询向量 + 候选向量（超时/没装 Ollama 时自然退化成纯 BM25）
        q_vec: List[float] = []
        vec_map: Dict[int, List[float]] = {}
        coss: Dict[int, float] = {}
        if text and self.setting("embedding_enabled"):
            q_vec = self._query_vec(ctx)
            if q_vec:
                coss = self._vec_cosines(q_vec, [int(r["id"]) for r in rows])
        vec_weight = max(0.0, min(0.9, float(self.setting("vec_weight", 0.45) or 0.0)))
        vec_min_cos = float(self.setting("vec_min_cos", 0.45) or 0.0)
        vec_min_margin = float(self.setting("vec_min_margin", 0.08) or 0.0)
        min_terms = int(self.setting("min_matched_terms", 1) or 0)
        infos = set(_informative_terms(text)) if text else set()
        qchars = set(_query_chars(text)) if text else set()

        # 语义闸：只有"明显比其它候选更像"的那条才允许在没关键词的情况下入选。
        best_cos = 0.0
        vec_pass = False
        vec_pass_ids: set = set()
        if q_vec and coss:
            ranked = sorted(coss.values(), reverse=True)
            best_cos = ranked[0] if ranked else 0.0
            second = ranked[1] if len(ranked) > 1 else 0.0
            margin = best_cos - second
            mode = str(self.setting("recall_vec_gate", "rank") or "rank").lower()
            if mode == "strict":
                # 旧行为：绝对门槛 0.82 + top 必须领先第二名（A/B 用）
                vec_pass = best_cos >= max(vec_min_cos, 0.82) and margin >= vec_min_margin
            else:
                # rank 模式：只看"和 top 差多少"，bge-m3 中文余弦普遍偏平，
                # 绝对 0.82 会把「换个说法」全挡掉（见 work/recall_eval.py 的 arm 对比）
                topk = max(1, int(self.setting("recall_vec_gate_topk", 3) or 3))
                floor = float(self.setting("recall_vec_gate_floor", 0.60) or 0.0)
                window = float(self.setting("recall_vec_gate_window", 0.12) or 0.0)
                cutoff = max(floor, best_cos - window)
                for mid, cos in sorted(coss.items(), key=lambda kv: (-kv[1], kv[0]))[:topk]:
                    if cos >= cutoff and (best_cos - cos) <= window + 1e-9:
                        vec_pass_ids.add(int(mid))
                vec_pass = bool(vec_pass_ids) and min(margin, 1.0) >= 0.0
        return {
            "contents": contents, "q_tokens": q_tokens, "docs": docs, "raw": raw,
            "q_vec": q_vec, "vec_map": vec_map, "vec_weight": vec_weight,
            "vec_min_cos": vec_min_cos, "vec_min_margin": vec_min_margin,
            "min_terms": min_terms, "infos": infos, "qchars": qchars,
            "coss": coss, "best_cos": best_cos, "vec_pass": vec_pass,
            "vec_pass_ids": vec_pass_ids,
            "entity_pass_ids": ctx.get("entity_ids") or set(),
        }

    def _recall_score(self, ctx, rows, sp):
        """召回第 2 段（打分）：逐条综合打分；公式已抽到 recall_pipeline.score_rows。"""
        return recall_pipeline.score_rows(rows, sp, ctx)

    def _recall_diversify(self, ctx, scored, rows, docs):
        """召回第 3 段（多样性）：注入去重 / 多样化，同一件事只留最相关那条。"""
        tokens_by_id = {int(row["id"]): set(docs[idx]) for idx, row in enumerate(rows)}
        div_ratio = float(self.setting("diversify_ratio", 0.75) or 0.0)
        return recall_pipeline.pick_diverse(scored, tokens_by_id, ctx["limit"], div_ratio)


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
        """召回第 4 段（兜底）：没有关键词可匹配时，按「重要度 + 新鲜度」给几条最值得说的。"""
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
        picked = recall_pipeline.rank_meta_fallback(
            rows, now, half_life_days, importance_decay, limit
        )
        if picked:
            with self.lock:
                self.conn.executemany(
                    "UPDATE memories SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
                    [(now, item["id"]) for item in picked],
                )
                self.conn.commit()
        return picked
