# -*- coding: utf-8 -*-
"""models/dao.py —— 记忆领域的高频 SQL：写入、去重查询、软删除、归档、向量、摘要拆分。

只拼 SQL、只搬运行；不判重要度、不做打分、不决定"该不该合并"。
业务判断全部在 services/*_engine.py。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

try:
    from ..utils.text_process import _bot_filter, _from_vec, _to_vec, _now
except ImportError:  # 顶层导入（测试/脚本）
    from utils.text_process import _bot_filter, _from_vec, _to_vec, _now


class MemoryDao:
    def __init__(self, db):
        self.db = db

    @property
    def conn(self) -> sqlite3.Connection:
        return self.db.conn

    @property
    def lock(self):
        return self.db.lock

    # ---------- 写 ----------
    def insert(self, bot_id: str, uid: str, pid: int, platform: str, scope: str,
               group_id: str, kind: str, subject: str, content: str, importance: float,
               hit_count: int, created_at: int, last_hit_at: int, idem_key: str) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO memories(bot_id, uid, pid, platform, scope, group_id, kind, subject, content, "
                "importance, hit_count, created_at, last_hit_at, idem_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bot_id, uid, int(pid), platform, scope, group_id, kind, subject, content,
                 importance, hit_count, created_at, last_hit_at, idem_key),
            )
            self.conn.commit()
        return int(cur.lastrowid or 0)

    def find_active_by_idem(self, bot_id: str, uid: str, scope: str, group_id: str,
                            idem_key: str) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT id, content, importance FROM memories "
                "WHERE bot_id=? AND uid=? AND scope=? AND group_id=? AND idem_key=? "
                "AND COALESCE(status,'active')='active'",
                (bot_id, uid, scope, group_id, idem_key),
            ).fetchone()

    def touch(self, memory_id: int, importance: float, now: int, subject: str) -> None:
        """同一件事又说一遍：抬重要度、刷新时间、合并计数 +1，不新增行。"""
        with self.lock:
            self.conn.execute(
                "UPDATE memories SET importance=MAX(importance, ?), last_hit_at=?, "
                "merged_count=merged_count+1, "
                "subject=CASE WHEN ?='bot' THEN 'bot' ELSE subject END WHERE id=?",
                (importance, now, subject, int(memory_id)),
            )
            self.conn.commit()

    def merge_content(self, memory_id: int, content: str, importance: float, now: int,
                      subject: str, keep_existing: bool) -> None:
        """近重复（换标点/语序）：把正文并到已有那条上。"""
        with self.lock:
            if keep_existing:
                self.conn.execute(
                    "UPDATE memories SET importance=MAX(importance, ?), "
                    "last_hit_at=?, merged_count=merged_count+1 WHERE id=?",
                    (importance, now, int(memory_id)),
                )
            else:
                self.conn.execute(
                    "UPDATE memories SET content=?, importance=MAX(importance, ?), "
                    "last_hit_at=?, merged_count=merged_count+1, "
                    "subject=CASE WHEN ?='bot' THEN 'bot' ELSE subject END WHERE id=?",
                    (content, importance, now, subject, int(memory_id)),
                )
            self.conn.commit()

    def recent(self, bot_id: str, uid: str, scope: str, group_id: str,
               limit: int = 200) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT id, content FROM memories WHERE bot_id=? AND uid=? AND scope=? "
                "AND group_id=? AND COALESCE(status,'active')='active' "
                "ORDER BY last_hit_at DESC LIMIT ?",
                (bot_id, uid, scope, group_id, int(limit)),
            ).fetchall()

    def active_with_slots(self, bot_id: str, uid: str, scope: str, group_id: str,
                          exclude_id: int, limit: int = 300) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT id, content, created_at, status FROM memories "
                "WHERE bot_id=? AND uid=? AND scope=? AND group_id=? AND id!=? "
                "AND COALESCE(status,'active')='active' "
                "ORDER BY created_at DESC LIMIT ?",
                (bot_id, uid, scope, group_id, int(exclude_id), int(limit)),
            ).fetchall()

    def supersede(self, new_id: int, old_ids: List[int]) -> None:
        if not old_ids:
            return
        with self.lock:
            self.conn.executemany(
                "UPDATE memories SET status='superseded', superseded_by=? WHERE id=?",
                [(int(new_id), int(old_id)) for old_id in old_ids],
            )
            self.conn.commit()

    def mark_superseded(self, memory_id: int, by_id: int = 0) -> bool:
        try:
            with self.lock:
                self.conn.execute(
                    "UPDATE memories SET status='superseded', superseded_by=? WHERE id=?",
                    (int(by_id or 0), int(memory_id)),
                )
                self.conn.commit()
            return True
        except Exception:
            return False

    def delete(self, bot_id: str, memory_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM memories WHERE bot_id=? AND id=?",
                (str(bot_id), int(memory_id)),
            )
            if cur.rowcount:
                # 向量表是独立表，不跟着删会一直堆积（AUTOINCREMENT 不复用 id，不会串数据）
                try:
                    self.conn.execute("DELETE FROM memory_vec WHERE memory_id=?", (int(memory_id),))
                except Exception:
                    pass
            self.conn.commit()
            return bool(cur.rowcount)

    # ---------- 读 ----------
    def list_by_person(self, bot_id: str, uid: str, pid: int, limit: int) -> List[Dict[str, Any]]:
        where, params = _bot_filter(bot_id)
        with self.lock:
            sql = (
                "SELECT id, uid, pid, platform, content, kind, subject, importance, created_at, "
                "hit_count, scope, group_id "
                "FROM memories WHERE 1=1" + where
            )
            if pid or uid:
                if pid:
                    # 按人取：有 pid 就按 pid（老行 pid=0 再用 uid 兜底），没 pid 就按 uid
                    sql += " AND (pid=? OR (COALESCE(pid,0)=0 AND uid=?))"
                    params.extend([int(pid), str(uid)])
                else:
                    sql += " AND uid=?"
                    params.append(str(uid))
            sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
            params.append(max(1, min(int(limit), 500)))
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def count_active(self, bot_id: str, uid: str, pid: int) -> int:
        with self.lock:
            if pid:
                row = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM memories WHERE bot_id=? AND (pid=? OR "
                    "(COALESCE(pid,0)=0 AND uid=?)) AND COALESCE(status,'active')='active'",
                    (bot_id, pid, uid),
                ).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM memories WHERE bot_id=? AND uid=? "
                    "AND COALESCE(status,'active')='active'",
                    (bot_id, uid),
                ).fetchone()
        return int(row["n"]) if row else 0

    def recall_candidates(self, base: str, params: List[Any], text: str,
                          pool_size: int) -> List[sqlite3.Row]:
        """子串命中 + 最近活跃两层粗筛；这里只取候选，排序交给引擎。"""
        rows: List[sqlite3.Row] = []
        with self.lock:
            if text:
                rows = self.conn.execute(
                    base + " AND content LIKE ? ORDER BY last_hit_at DESC, created_at DESC LIMIT ?",
                    list(params) + ["%" + text + "%", int(pool_size)],
                ).fetchall()
        return rows

    def recent_candidates(self, base: str, params: List[Any],
                          recent_pool: int) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                base + " ORDER BY last_hit_at DESC, created_at DESC LIMIT ?",
                list(params) + [int(recent_pool)],
            ).fetchall()

    def digest_candidates(self, base: str, params: List[Any], date_tag: str) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                base + " AND kind='daily_digest' AND content LIKE ? LIMIT 3",
                list(params) + ["%" + date_tag + "%"],
            ).fetchall()

    def bump_hits(self, memory_ids: List[int], now: int) -> None:
        if not memory_ids:
            return
        with self.lock:
            self.conn.executemany(
                "UPDATE memories SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
                [(now, int(mid)) for mid in memory_ids],
            )
            self.conn.commit()

    # ---------- 归档 / 压缩 ----------
    def archive_candidates(self, where: str, params: List[Any], max_importance: float,
                           cutoff: int) -> List[int]:
        sql = ("SELECT id FROM memories WHERE 1=1" + where +
               " AND COALESCE(status,'active')='active' AND importance<=?"
               " AND COALESCE(hit_count,0)<2 AND COALESCE(last_hit_at,created_at)<?"
               " AND COALESCE(kind,'')!='pinned'")
        with self.lock:
            rows = self.conn.execute(
                sql, list(params) + [float(max_importance), int(cutoff)]
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def mark_archived(self, ids: List[int]) -> None:
        if not ids:
            return
        with self.lock:
            self.conn.executemany(
                "UPDATE memories SET status='archived' WHERE id=?",
                [(i,) for i in ids],
            )
            self.conn.commit()

    def long_summaries(self, where: str, params: List[Any], cap: int,
                       limit: int) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT id, bot_id, uid, content, kind, subject, importance, created_at, last_hit_at, "
                "COALESCE(scope,'private') AS scope, COALESCE(group_id,'') AS group_id "
                "FROM memories WHERE 1=1" + where +
                " AND COALESCE(status,'active')='active'"
                " AND kind IN ('conversation_summary','daily_digest')"
                " AND length(content)>? ORDER BY length(content) DESC LIMIT ?",
                list(params) + [int(cap), max(1, int(limit))],
            ).fetchall()

    def update_content(self, memory_id: int, content: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE memories SET content=? WHERE id=?", (content, int(memory_id)))
            self.conn.commit()

    def insert_piece(self, row: sqlite3.Row, content: str, now: int) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO memories(bot_id, uid, scope, group_id, kind, subject, content, "
                "importance, hit_count, created_at, last_hit_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (str(row["bot_id"]), str(row["uid"]), str(row["scope"]),
                 str(row["group_id"]), str(row["kind"]),
                 str(row["subject"] or "user"), content,
                 float(row["importance"] or 0.5), 0,
                 int(row["created_at"] or now), int(row["last_hit_at"] or now)),
            )
            self.conn.commit()
        return int(cur.lastrowid or 0)

    def delete_by_keyword(self, bot_id: str, keyword: str) -> int:
        """物理删除包含关键词的记忆（管理员 /遗忘）。"""
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM memories WHERE bot_id=? AND content LIKE ?",
                (str(bot_id), "%" + str(keyword) + "%"),
            )
            self.conn.commit()
            return int(cur.rowcount or 0)

    def active_rows(self, where: str, params: List[Any], limit: int) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT id, uid, content, kind, importance, created_at, hit_count, last_hit_at, "
                "COALESCE(scope,'private') AS scope, COALESCE(group_id,'') AS group_id "
                "FROM memories WHERE 1=1" + where +
                " AND COALESCE(status,'active')='active' ORDER BY created_at DESC LIMIT ?",
                list(params) + [max(1, int(limit))],
            ).fetchall()

    def merge_summary(self, keep_id: int, content: str, absorbed: int, importance: float) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE memories SET content=?, merged_count=COALESCE(merged_count,1)+?, "
                "importance=MAX(importance, ?) WHERE id=?",
                (content, int(absorbed), float(importance), int(keep_id)),
            )
            self.conn.commit()

    def mark_merged(self, keep_id: int, ids: List[int]) -> None:
        if not ids:
            return
        with self.lock:
            self.conn.executemany(
                "UPDATE memories SET status='merged', superseded_by=? WHERE id=?",
                [(int(keep_id), int(i)) for i in ids],
            )
            self.conn.commit()

    # ---------- 向量 ----------
    def store_vec(self, memory_id: int, model: str, vec: List[float]) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO memory_vec(memory_id, model, dim, vec, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(memory_id) DO UPDATE SET model=excluded.model, dim=excluded.dim, "
                "vec=excluded.vec, updated_at=excluded.updated_at",
                (int(memory_id), model, len(vec), _from_vec(vec), _now()),
            )
            self.conn.commit()

    def load_vecs(self, ids: List[int]) -> Dict[int, List[float]]:
        if not ids:
            return {}
        out: Dict[int, List[float]] = {}
        try:
            marks = ",".join("?" for _ in ids)
            with self.lock:
                rows = self.conn.execute(
                    "SELECT memory_id, dim, vec FROM memory_vec WHERE memory_id IN (%s)" % marks,
                    list(ids),
                ).fetchall()
            for row in rows:
                vec = _to_vec(row["vec"], int(row["dim"] or 0))
                if vec:
                    out[int(row["memory_id"])] = vec
        except Exception:
            return {}
        return out

    def missing_vecs(self, limit: int) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT m.id, m.content FROM memories m "
                "LEFT JOIN memory_vec v ON v.memory_id=m.id "
                "WHERE v.memory_id IS NULL ORDER BY m.id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()

    def vec_stats(self) -> Dict[str, int]:
        try:
            with self.lock:
                have = int(self.conn.execute("SELECT COUNT(*) AS n FROM memory_vec").fetchone()["n"])
                total = int(self.conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])
        except Exception:
            have, total = 0, 0
        return {"vectors": have, "memories": total}

    # ---------- 来源平台回填 ----------
    def platform_blank_rows(self, clause: str, params: List[Any], limit: int) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                "SELECT id, bot_id, uid, pid, kind, content, created_at FROM memories "
                "WHERE COALESCE(platform,'')=''" + clause + " ORDER BY id DESC LIMIT ?",
                list(params) + [max(1, min(int(limit), 20000))],
            ).fetchall()

    def message_platform_by_content(self, bot_id: str, content: str) -> Optional[str]:
        with self.lock:
            hit = self.conn.execute(
                "SELECT platform FROM messages WHERE bot_id=? AND COALESCE(platform,'')!='' "
                "AND content=? LIMIT 1", (bot_id, content),
            ).fetchone()
        return str(hit["platform"]).strip() if hit and str(hit["platform"] or "").strip() else None

    def links_for(self, bot_id: str, pid: int, uid: str) -> List[sqlite3.Row]:
        with self.lock:
            if pid:
                return self.conn.execute(
                    "SELECT platform, uid FROM person_links WHERE bot_id=? AND pid=?",
                    (bot_id, int(pid))).fetchall()
            return self.conn.execute(
                "SELECT platform, uid FROM person_links WHERE bot_id=? AND uid=?",
                (bot_id, str(uid))).fetchall()

    def near_platforms(self, bot_id: str, pid: int, uid: str, created_at: int,
                       window_sec: int) -> List[sqlite3.Row]:
        with self.lock:
            if pid:
                return self.conn.execute(
                    "SELECT DISTINCT platform FROM messages WHERE bot_id=? AND pid=? "
                    "AND COALESCE(platform,'')!='' AND ABS(created_at-?)<=?",
                    (bot_id, int(pid), int(created_at), int(window_sec))).fetchall()
            return self.conn.execute(
                "SELECT DISTINCT platform FROM messages WHERE bot_id=? AND uid=? "
                "AND COALESCE(platform,'')!='' AND ABS(created_at-?)<=?",
                (bot_id, str(uid), int(created_at), int(window_sec))).fetchall()

    def set_platform(self, updates: List[tuple]) -> None:
        if not updates:
            return
        with self.lock:
            self.conn.executemany(
                "UPDATE memories SET platform=? WHERE id=? AND COALESCE(platform,'')=''",
                updates,
            )
            self.conn.commit()
