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
    from ..utils.text_process import _bot_filter, _now, _recall_tokens, logger
except ImportError:  # 顶层导入（测试/脚本）
    from models.dao import MemoryDao
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
            if apply:
                # 同一批摘要各有信息：把正文拼到保留的那条上，再标记别条（不丢内容）
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
                if absorbed:
                    self.dao.merge_summary(
                        int(keep["id"]), merged_text, len(absorbed),
                        max([float(r["importance"] or 0) for r in items]),
                    )
                    self.dao.mark_merged(int(keep["id"]), [int(r["id"]) for r in absorbed])
            merged += len(absorbed) if apply else 0

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
        return {"merged_summaries": merged, "merged_near_duplicates": dedup,
                "split_summaries": split_info.get("summaries", 0),
                "split_pieces": split_info.get("created", 0),
                "applied": bool(apply)}
