# -*- coding: utf-8 -*-
"""services/entity_graph.py —— 通用记忆图谱：实体抽取 + 共现边 + 实体路召回。

和 graph_recall（只认人物）互补：这里从记忆正文里抽名词性实体（jieba 词性可选，
没有 jieba 就退回「中文串按标点切 2–8 字」），写进 entities/memory_entities，
同一条记忆里的实体两两连边（entity_edges）。召回时把查询里的实体命中、再扩一跳
共现实体，取相关记忆 id。

纯规则、不调模型、不联网。表结构由 ensure_schema() 幂等创建（不碰 SCHEMA_SQL 版本号）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

MAX_ENTITIES = 6
#: 抽出来也不当实体的词（问句壳/泛指）
STOP_ENTITIES: Set[str] = {
    "什么", "时候", "事情", "东西", "最近", "今天", "明天", "昨天", "现在",
    "记得", "知道", "告诉", "感觉", "觉得", "可以", "应该", "然后", "因为",
    "所以", "但是", "如果", "这个", "那个", "我们", "他们", "自己", "一个",
}

QUOTED = re.compile(r"[「『《]([^」』》]{2,12})[」』》]")
CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,8}")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT DEFAULT 'noun',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(bot_id, name)
);
CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    weight REAL DEFAULT 1.0,
    PRIMARY KEY (memory_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_mem_ent_entity ON memory_entities(entity_id);
CREATE TABLE IF NOT EXISTS entity_edges (
    bot_id TEXT NOT NULL,
    a_id INTEGER NOT NULL,
    b_id INTEGER NOT NULL,
    weight INTEGER DEFAULT 1,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (bot_id, a_id, b_id)
);
"""

_ensured: Set[str] = set()


def ensure_schema(dao) -> None:
    # 用库路径当 key：用对象 id 会在临时库被回收后撞号，导致新库跳过建表
    key = str(getattr(getattr(dao, "db", None), "path", "") or id(dao))
    if key in _ensured:
        return
    with dao.lock:
        dao.conn.executescript(SCHEMA_SQL)
        dao.conn.commit()
    _ensured.add(key)


def _python_nouns(text: str) -> List[str]:
    try:
        import jieba.posseg as pseg  # 可选依赖：AstrBot 自带 jieba
    except Exception:  # cleanup: 没装 jieba 就走 CJK 串兜底
        return []
    try:
        words = [(word, flag) for word, flag in pseg.cut(str(text or ""))]
    except Exception:  # cleanup: 分词失败不能影响写记忆
        return []
    out: List[str] = []
    index = 0
    while index < len(words):
        word, flag = words[index]
        # 「小周」常被切成 小(a)+周(nr)：单字名词/形容词和后面的名词拼一下
        if len(word) == 1 and flag.startswith(("a", "n")):
            nxt = words[index + 1] if index + 1 < len(words) else None
            if nxt and len(nxt[0]) <= 3 and nxt[1].startswith("n"):
                merged = word + nxt[0]
                if 2 <= len(merged) <= 4:
                    out.append(merged)
                    index += 2
                    continue
        if flag.startswith(("n", "nr", "ns", "nt", "nz", "vn")) and 2 <= len(word) <= 8:
            out.append(word)
        index += 1
    return out


def extract_entities(text: str, known_names: Sequence[str] = (), limit: int = MAX_ENTITIES) -> List[str]:
    """正文 → 实体名列表（人物名字优先，其次引号词、jieba 名词、CJK 串兜底）。"""
    body = str(text or "")
    if not body.strip():
        return []
    found: List[str] = []

    def _take(name: str) -> None:
        name = str(name or "").strip()
        if len(name) < 2 or name in STOP_ENTITIES or name.isdigit():
            return
        if name not in found:
            found.append(name)

    for name in known_names or ():
        if name and str(name) in body:
            _take(str(name))
    for name in QUOTED.findall(body):
        _take(name)
    for name in _python_nouns(body):
        _take(name)
    if not found:
        for name in CJK_RUN.findall(body):
            _take(name)
    return found[:max(1, int(limit))]


def link_memory(dao, bot_id: str, memory_id: int, text: str,
                known_names: Sequence[str] = ()) -> List[str]:
    """把一条记忆挂到实体上，并给同记忆内的实体两两加边（幂等）。"""
    names = extract_entities(text, known_names)
    if not names or not memory_id:
        return []
    ensure_schema(dao)
    import time
    now = int(time.time())
    with dao.lock:
        conn = dao.conn
        ids: List[int] = []
        for name in names:
            conn.execute(
                "INSERT INTO entities(bot_id,name,kind,created_at,updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(bot_id,name) DO UPDATE SET "
                "updated_at=excluded.updated_at",
                (str(bot_id), name, "noun", now, now),
            )
            row = conn.execute(
                "SELECT id FROM entities WHERE bot_id=? AND name=?", (str(bot_id), name)
            ).fetchone()
            if row:
                ids.append(int(row["id"]))
        for entity_id in ids:
            conn.execute(
                "INSERT OR IGNORE INTO memory_entities(memory_id,entity_id,weight) "
                "VALUES(?,?,1.0)", (int(memory_id), entity_id),
            )
        for index, left in enumerate(ids):
            for right in ids[index + 1:]:
                a, b = (left, right) if left < right else (right, left)
                conn.execute(
                    "INSERT INTO entity_edges(bot_id,a_id,b_id,weight,updated_at) "
                    "VALUES(?,?,?,1,?) ON CONFLICT(bot_id,a_id,b_id) DO UPDATE SET "
                    "weight=entity_edges.weight+1, updated_at=excluded.updated_at",
                    (str(bot_id), a, b, now),
                )
        conn.commit()
    return names


def memories_for_query(conn, lock, bot_id: str, text: str, limit: int = 10) -> List[int]:
    """查询里的实体 → 直接命中 + 一跳共现 → 记忆 id（按共现权重排）。"""
    names = extract_entities(text)
    if not names:
        return []
    marks = ",".join("?" for _ in names)
    try:
        with lock:
            rows = conn.execute(
                "SELECT id FROM entities WHERE bot_id=? AND name IN (" + marks + ")",
                [str(bot_id)] + list(names),
            ).fetchall()
            entity_ids = [int(row["id"]) for row in rows]
            if not entity_ids:
                return []
            emarks = ",".join("?" for _ in entity_ids)
            edges = conn.execute(
                "SELECT b_id AS eid FROM entity_edges WHERE bot_id=? AND a_id IN (" + emarks + ") "
                "UNION SELECT a_id AS eid FROM entity_edges WHERE bot_id=? AND b_id IN (" + emarks + ")",
                [str(bot_id)] + entity_ids + [str(bot_id)] + entity_ids,
            ).fetchall()
            for row in edges:
                if int(row["eid"]) not in entity_ids:
                    entity_ids.append(int(row["eid"]))
            smarks = ",".join("?" for _ in entity_ids)
            mems = conn.execute(
                "SELECT memory_id, SUM(weight) AS score FROM memory_entities "
                "WHERE entity_id IN (" + smarks + ") GROUP BY memory_id "
                "ORDER BY score DESC, memory_id DESC LIMIT ?",
                list(entity_ids) + [max(1, int(limit))],
            ).fetchall()
        return [int(row["memory_id"]) for row in mems]
    except Exception:  # cleanup: 图谱表缺失/查询失败 → 这一路不贡献候选
        return []


def backfill(dao, bot_id: str, limit: int = 5000) -> int:
    """给还没有实体链接的老记忆补图谱（夜间维护跑一次）。"""
    ensure_schema(dao)
    with dao.lock:
        rows = dao.conn.execute(
            "SELECT m.id, m.content FROM memories m "
            "LEFT JOIN memory_entities e ON e.memory_id=m.id "
            "WHERE m.bot_id=? AND e.memory_id IS NULL "
            "AND COALESCE(m.status,'active')='active' ORDER BY m.id DESC LIMIT ?",
            (str(bot_id), max(1, int(limit))),
        ).fetchall()
    count = 0
    for row in rows:
        if link_memory(dao, bot_id, int(row["id"]), str(row["content"] or "")):
            count += 1
    return count


def prune_orphans(dao, bot_id: str) -> int:
    """删除孤立实体节点和悬空边（夜间维护跑一次）。

    孤立实体 = entities 里没有任何 memory_entities 引用的节点；
    悬空边   = entity_edges 里 a_id 或 b_id 已不在 entities 里的边。
    记忆被软删除（status != active）时 memory_entities 仍保留，
    所以只清「完全没有引用」的实体，不会误删活跃记忆的关联。
    """
    ensure_schema(dao)
    deleted_entities = 0
    deleted_edges = 0
    with dao.lock:
        conn = dao.conn
        # 1. 找孤立实体（没有任何 memory_entities 引用）
        orphan_ids = [
            row[0] for row in conn.execute(
                "SELECT e.id FROM entities e "
                "WHERE e.bot_id=? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM memory_entities me WHERE me.entity_id=e.id"
                ")",
                (str(bot_id),),
            ).fetchall()
        ]
        if orphan_ids:
            marks = ",".join("?" for _ in orphan_ids)
            # 先删悬空边（引用了孤立实体的边）
            conn.execute(
                f"DELETE FROM entity_edges WHERE bot_id=? "
                f"AND (a_id IN ({marks}) OR b_id IN ({marks}))",
                [str(bot_id)] + orphan_ids + orphan_ids,
            )
            # 再删孤立实体
            conn.execute(
                f"DELETE FROM entities WHERE id IN ({marks})",
                orphan_ids,
            )
            deleted_entities = len(orphan_ids)
        # 2. 清除悬空边（a_id 或 b_id 不在 entities 里）
        result = conn.execute(
            "DELETE FROM entity_edges "
            "WHERE bot_id=? "
            "AND (a_id NOT IN (SELECT id FROM entities) "
            "  OR b_id NOT IN (SELECT id FROM entities))",
            (str(bot_id),),
        )
        deleted_edges += result.rowcount
        conn.commit()
    return deleted_entities
