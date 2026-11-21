# -*- coding: utf-8 -*-
"""services/compression_engine.py —— 防膨胀：长摘要拆条、同天摘要合并、近重复标过期、90 天归档。

原则：只改 status（merged / superseded / archived），不物理删除正文；
合并时正文拼接，超预算就停手，绝不带病标 merged。

Phase 13 P2：纯规则搬到 services/compression_rules.py（拆条/分组/合并计划/近重复对）
与 services/fact_rules.py（静态事实分组/正负切分/合并正文/降权候选），本文件只留
SQL、设置读取和写回。``_split_long_text`` 保留为别名，老测试仍可 import。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

try:
    from ..models.dao import MemoryDao
    from ..services import compression_rules, fact_rules
    from ..services import consolidation, memory_decay
    from ..utils.text_process import _bot_filter, _now, _recall_tokens
except ImportError:  # 顶层导入（测试/脚本）
    from models.dao import MemoryDao
    from services import compression_rules, consolidation, fact_rules, memory_decay
    from utils.text_process import _bot_filter, _now, _recall_tokens

#: 兼容别名：拆条规则已搬到 compression_rules
_split_long_text = compression_rules.split_long_text


class CompressionEngine:
    def __init__(self, db, settings, memory=None):
        self.db = db
        self.settings = settings
        self.memory = memory
        self.dao = MemoryDao(db)

    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    @property
    def lock(self):
        return self.db.lock

    def archive_stale(self, bot_id: str, days: int = 90, max_importance: float = 0.3,
                      apply: bool = True) -> Dict[str, Any]:
        """长期没被想起、又不重要的记忆归档（不删除，只是不再参与召回）。"""
        now = _now()
        cutoff = now - max(1, int(days)) * 86400
        where, params = _bot_filter(bot_id)
        ids = self.dao.archive_candidates(where, params, max_importance, cutoff)
        # 类型化 TTL：流水账/机器记忆 30 天就退场，别和长期事实抢召回
        noise_cutoff = now - int(memory_decay.NOISE_TTL_DAYS) * 86400
        noise = self.dao.archive_noise_candidates(
            where, params, memory_decay.noise_kind_list(), memory_decay.NOISE_MAX_IMPORTANCE,
            noise_cutoff,
        )
        merged = sorted(set(ids) | set(noise))
        if merged and apply:
            self.dao.mark_archived(merged)
        return {"candidate": len(merged), "archived": len(merged) if apply else 0,
                "noise": len(noise), "ids": merged[:50]}

    def split_long_summaries(self, bot_id: str, max_chars: int = 0, apply: bool = True,
                             limit: int = 200) -> Dict[str, Any]:
        """把超过上限的摘要按句子拆成多条（正文一个字不丢）。"""
        cap = int(max_chars or self.setting("summary_merge_max_chars", 800) or 0)
        if cap <= 0:
            return {"summaries": 0, "created": 0, "applied": bool(apply)}
        where, params = _bot_filter(bot_id)
        rows = self.dao.long_summaries(where, params, cap, limit)
        summaries = 0
        created = 0
        for row in rows:
            pieces = compression_rules.split_long_text(row["content"], cap)
            if len(pieces) < 2:
                continue
            summaries += 1
            if not apply:
                created += len(pieces) - 1
                continue
            with self.lock:
                self.dao.conn.execute(
                    "UPDATE memories SET content=? WHERE id=?",
                    (pieces[0], int(row["id"])),
                )
                for piece in pieces[1:]:
                    new_id = self.dao.insert_piece(row, piece, _now())
                    created += 1
                    if new_id and self.memory is not None:
                        self.memory._enqueue_embedding(new_id, piece)
                self.dao.conn.commit()
            if self.memory is not None:
                self.memory._enqueue_embedding(int(row["id"]), pieces[0])
        return {"summaries": summaries, "created": created, "applied": bool(apply)}

    def compress_facts(self, bot_id: str, apply: bool = True,
                       rows=None, limit: int = 600) -> Dict[str, Any]:
        """事实级压缩：同一事实合并细节、负状态覆盖、60 天低权重衰减。

        只处理静态事实（偏好/人设/关系/资产），消费/交易/摘要永远不动。
        """
        if rows is None:
            where, params = _bot_filter(bot_id)
            rows = self.dao.active_rows(where, params, limit)
        groups: Dict[Any, List[sqlite3.Row]] = fact_rules.group_static_facts(rows)
        merged = expired = 0
        for items in groups.values():
            if len(items) < 2:
                continue
            if not apply:
                merged += len(items) - 1
                continue
            neg, pos = fact_rules.split_neg_pos(items)
            if neg and pos:
                keep = fact_rules.newest(neg)
                if keep is None or keep.get("id") is None:
                    continue
                old_ids = [int(r["id"]) for r in pos]
                with self.lock:
                    self.dao.conn.executemany(
                        "UPDATE memories SET status='expired', superseded_by=? WHERE id=?",
                        [(int(keep["id"]), old_id) for old_id in old_ids],
                    )
                    self.dao.conn.commit()
                expired += len(old_ids)
                items = [r for r in items if int(r["id"]) not in set(old_ids)]
                if len(items) < 2:
                    continue
            base = fact_rules.pick_base(items)
            text = fact_rules.merged_text(items, base)
            importance = fact_rules.bumped_importance(items)
            with self.lock:
                self.dao.conn.execute(
                    "UPDATE memories SET content=?, importance=?, "
                    "merged_count=merged_count+? WHERE id=?",
                    (text, importance, len(items) - 1, int(base["id"])),
                )
                self.dao.conn.executemany(
                    "UPDATE memories SET status='merged', superseded_by=? WHERE id=?",
                    [(int(base["id"]), int(r["id"])) for r in items if r is not base],
                )
                self.dao.conn.commit()
            merged += len(items) - 1
        # 遗忘曲线：60 天没被检索/提及的低权重静态事实，慢慢降权
        decayed = 0
        cutoff = _now() - 60 * 86400
        if apply:
            for importance, row in fact_rules.decay_targets(rows, cutoff):
                with self.lock:
                    self.dao.conn.execute(
                        "UPDATE memories SET importance=? WHERE id=?",
                        (max(0.1, importance - 0.05), int(row["id"])),
                    )
                    self.dao.conn.commit()
                decayed += 1
        return {"merged": merged, "expired": expired, "decayed": decayed}

    def consolidate_noise(self, bot_id: str, apply: bool = True) -> Dict[str, Any]:
        """旧流水巩固：7 天前的同天同会话流水（≥3 条）并成一条，原文不丢。"""
        days = float(self.setting("lifecycle_consolidate_days", 7) or 7)
        min_items = int(self.setting("lifecycle_consolidate_min", 3) or 3)
        where, params = _bot_filter(bot_id)
        rows = [dict(r) for r in self.dao.active_rows(where, params, 2000)]
        groups = consolidation.plan(rows, _now(), days=days, min_items=min_items)
        merged = 0
        if apply:
            for group in groups:
                keep_id = int(group["keep_id"])
                others = [i for i in group["ids"] if i != keep_id]
                if not others:
                    continue
                self.dao.merge_summary(keep_id, group["content"], len(others),
                                       float(group["importance"]))
                self.dao.mark_merged(keep_id, others)
                merged += len(others)
        return {"groups": len(groups), "merged": merged, "applied": bool(apply)}

    def compress_memories(self, bot_id: str, apply: bool = True,
                          min_jaccard: float = 0.7, limit: int = 600) -> Dict[str, Any]:
        """周度整理：长摘要拆条、同一天摘要合并、近重复记忆标成过期（不删数据）。"""
        cap = int(self.setting("summary_merge_max_chars", 800) or 0)
        split_info = self.split_long_summaries(bot_id, cap, apply=bool(apply), limit=200)
        where, params = _bot_filter(bot_id)
        rows = self.dao.active_rows(where, params, limit)
        merged = 0
        # 按「谁 + 哪一天 + 哪个会话」分组：私聊和群聊、两个人的同一天不能并成一条
        groups: Dict[Any, List[sqlite3.Row]] = {}
        for row in rows:
            if str(row["kind"] or "") != "conversation_summary":
                continue
            key = compression_rules.summary_group_key(
                row["uid"], row["scope"], row["group_id"], row["content"]
            )
            if key:
                groups.setdefault(key, []).append(row)
        for items in groups.values():
            if len(items) < 2:
                continue
            keep, merged_body, absorbed = compression_rules.plan_summary_merge(items, cap)
            merged += len(absorbed)
            if apply and absorbed:
                self.dao.merge_summary(
                    int(keep["id"]), merged_body, len(absorbed),
                    max([float(r["importance"] or 0) for r in items]),
                )
                self.dao.mark_merged(int(keep["id"]), [int(r["id"]) for r in absorbed])

        # 通用近重复（只处理最近 limit 条，代价可控）；判定在 compression_rules 里
        token_rows = [
            (int(r["id"]), str(r["kind"] or ""), str(r["scope"] or "private"),
             str(r["group_id"] or ""), _recall_tokens(str(r["content"] or "")))
            for r in rows
        ]
        pairs = compression_rules.near_duplicate_pairs(token_rows, min_jaccard)
        dedup = len(pairs)
        if apply and pairs:
            with self.lock:
                for id_a, id_b in pairs:
                    self.dao.conn.execute(
                        "UPDATE memories SET status='merged', superseded_by=? WHERE id=?",
                        (id_a, id_b),
                    )
                self.dao.conn.commit()
        fact_info = self.compress_facts(bot_id, apply=apply, rows=rows)
        return {"merged_summaries": merged, "merged_near_duplicates": dedup,
                "merged_facts": fact_info.get("merged", 0),
                "expired_facts": fact_info.get("expired", 0),
                "decayed": fact_info.get("decayed", 0),
                "split_summaries": split_info.get("summaries", 0),
                "split_pieces": split_info.get("created", 0),
                "applied": bool(apply)}
