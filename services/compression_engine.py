# -*- coding: utf-8 -*-
"""services/compression_engine.py —— 防膨胀：长摘要拆条、同天摘要合并、近重复标过期、90 天归档。

原则：只改 status（merged / superseded / archived），不物理删除正文；
合并时正文拼接，超预算就停手，绝不带病标 merged。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List

try:
    from ..models.dao import MemoryDao
    from ..services import fact_dedupe
    from ..utils.text_process import _bot_filter, _now, _recall_tokens, logger
except ImportError:  # 顶层导入（测试/脚本）
    from models.dao import MemoryDao
    from services import fact_dedupe
    from utils.text_process import _bot_filter, _now, _recall_tokens, logger

# 句子边界：中文句号/问号/叹号/分号/换行；用 lookbehind 保留分隔符，拼回去还是原文
_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;\n])")


def _split_long_text(text: Any, limit: int) -> List[str]:
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
        cutoff = _now() - max(1, int(days)) * 86400
        where, params = _bot_filter(bot_id)
        ids = self.dao.archive_candidates(where, params, max_importance, cutoff)
        if ids and apply:
            self.dao.mark_archived(ids)
        return {"candidate": len(ids), "archived": len(ids) if apply else 0, "ids": ids[:50]}

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
            pieces = _split_long_text(row["content"], cap)
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
        groups: Dict[Any, List[sqlite3.Row]] = {}
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
        merged = expired = 0
        for items in groups.values():
            if len(items) < 2:
                continue
            if not apply:
                merged += len(items) - 1
                continue
            neg = [r for r in items if fact_dedupe.fact_state(str(r["content"] or "")) == "neg"]
            pos = [r for r in items if r not in neg]
            if neg and pos:
                keep = max(neg, key=lambda r: int(r["created_at"] or 0))
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
            base = max(items, key=lambda r: (float(r["importance"] or 0),
                                             len(str(r["content"] or ""))))
            text = str(base["content"] or "")
            for other in items:
                if other is base:
                    continue
                text = fact_dedupe.merge_fact(text, str(other["content"] or ""))
            top = max(float(r["importance"] or 0) for r in items)
            importance = min(1.0, top + 0.05 * (len(items) - 1))
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
            for row in rows:
                if not fact_dedupe.is_static_kind(str(row["kind"] or "")):
                    continue
                if int(row["last_hit_at"] or 0) > cutoff:
                    continue
                importance = float(row["importance"] or 0)
                if importance <= 0.2:
                    continue
                with self.lock:
                    self.dao.conn.execute(
                        "UPDATE memories SET importance=? WHERE id=?",
                        (max(0.1, importance - 0.05), int(row["id"])),
                    )
                    self.dao.conn.commit()
                decayed += 1
        return {"merged": merged, "expired": expired, "decayed": decayed}

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
            match = re.search(r"(\d{4}-\d{2}-\d{2})", str(row["content"] or ""))
            if match:
                key = (str(row["uid"] or ""), match.group(1),
                       str(row["scope"] or "private"), str(row["group_id"] or ""))
                groups.setdefault(key, []).append(row)
        for _day, items in groups.items():
            if len(items) < 2:
                continue
            items.sort(key=lambda r: (len(str(r["content"] or "")), int(r["created_at"] or 0)), reverse=True)
            keep = items[0]
            drop = items[1:]
            absorbed: List[sqlite3.Row] = []
            # 同一批摘要各有信息：把正文拼到保留的那条上，再标记别条（不丢内容）。
            # 预演（apply=False）也走同样的拼接，否则预演永远显示「会合并 0 条」。
            merged_text = str(keep["content"] or "")
            for row in drop:
                piece = str(row["content"] or "").strip()
                if not piece or piece in merged_text:
                    absorbed.append(row)  # 正文已经在保留行里，标 merged 不会丢内容
                    continue
                candidate = merged_text + "\n" + piece
                if len(candidate) > (cap or 4000):
                    break  # 超预算：这条和后面的都保持 active，绝不带病标 merged
                merged_text = candidate
                absorbed.append(row)
            merged += len(absorbed)
            if apply and absorbed:
                self.dao.merge_summary(
                    int(keep["id"]), merged_text, len(absorbed),
                    max([float(r["importance"] or 0) for r in items]),
                )
                self.dao.mark_merged(int(keep["id"]), [int(r["id"]) for r in absorbed])

        # 通用近重复（只处理最近 limit 条，代价可控）
        dedup = 0
        tokens = [(int(r["id"]), str(r["kind"] or ""), str(r["scope"] or "private"),
                   str(r["group_id"] or ""), _recall_tokens(str(r["content"] or "")))
                  for r in rows]
        with self.lock:
            # 集合只建一次：原来每比较一对都要重新 set() 两遍
            token_sets = [set(toks) for (_id, _kind, _scope, _group, toks) in tokens]
            for i in range(len(tokens)):
                id_a, kind_a, scope_a, group_a, tok_a = tokens[i]
                if len(tok_a) < 8:
                    continue
                for j in range(i + 1, len(tokens)):
                    id_b, kind_b, scope_b, group_b, tok_b = tokens[j]
                    if kind_a != kind_b:
                        continue
                    # 跨会话的近重复不合并：群里的那条接不到私聊行上
                    if scope_a != scope_b or group_a != group_b:
                        continue
                    if len(tok_b) < 8:
                        continue
                    if abs(len(tok_a) - len(tok_b)) > max(len(tok_a), len(tok_b)) * 0.5:
                        continue
                    # 双向都要像，且长度接近，避免"信息更少的短句被长句吞掉"
                    sa, sb = token_sets[i], token_sets[j]
                    jaccard = len(sa & sb) / float(len(sa | sb) or 1)
                    containment = len(sa & sb) / float(min(len(sa), len(sb)) or 1)
                    ratio = float(min(len(tok_a), len(tok_b))) / float(max(len(tok_a), len(tok_b)) or 1)
                    if jaccard < float(min_jaccard) or containment < 0.9 or ratio < 0.7:
                        continue
                    if apply:
                        self.dao.conn.execute(
                            "UPDATE memories SET status='merged', superseded_by=? WHERE id=?",
                            (id_a, id_b),
                        )
                    dedup += 1
            if apply and dedup:
                self.dao.conn.commit()
        fact_info = self.compress_facts(bot_id, apply=apply, rows=rows)
        return {"merged_summaries": merged, "merged_near_duplicates": dedup,
                "merged_facts": fact_info.get("merged", 0),
                "expired_facts": fact_info.get("expired", 0),
                "decayed": fact_info.get("decayed", 0),
                "split_summaries": split_info.get("summaries", 0),
                "split_pieces": split_info.get("created", 0),
                "applied": bool(apply)}
