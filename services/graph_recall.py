# -*- coding: utf-8 -*-
"""services/graph_recall.py —— 实体/关系路：先认人，再按人物取记忆。

文本路只认字面。用户问「我表哥叫什么」「团子最近怎么样」时，字面可能只出现在
人物档案和关系表里，回忆本身写的是「他今天…」。这一路用已经存在的数据补上：

    persons.name / person_links.name  -> 名字、别名
    relations(a_uid, b_uid, relation) -> 「谁是谁的谁」

命中实体后按 pid 取这个人的记忆；命中关系词时取关系另一头的人。
纯查询，不写库；结果并入候选池，再由既有打分/多样性收敛。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def resolve_entities(conn, lock, bot_id: str, uid: str, text: str,
                     limit: int = 6) -> Dict[str, List[Any]]:
    """从 text 里认出人物和关系，返回 {names, pids, matched}。

    * names：文本里出现的名字/别名（>=2 字，避免「他」这种撞词）；
    * pids：这些名字对应的 pid，加上「关系词」另一头的人；
    * matched：命中的原始词，便于排查。
    """
    body = str(text or "")
    if not body.strip():
        return {"names": [], "pids": [], "matched": []}
    names: List[str] = []
    pids: List[int] = []
    matched: List[str] = []
    try:
        with lock:
            people = conn.execute(
                "SELECT pid, name FROM persons WHERE bot_id=? AND COALESCE(name,'')<>''",
                (str(bot_id),),
            ).fetchall()
            links = conn.execute(
                "SELECT uid, pid, name FROM person_links WHERE bot_id=? "
                "AND COALESCE(name,'')<>''",
                (str(bot_id),),
            ).fetchall()
    except Exception:
        return {"names": [], "pids": [], "matched": []}

    seen = set()
    for row in people:
        name = str(row["name"] or "").strip()
        if len(name) >= 2 and name in body and name not in seen:
            seen.add(name)
            names.append(name)
            matched.append(name)
            pids.append(int(row["pid"]))
    for row in links:
        name = str(row["name"] or "").strip()
        if len(name) >= 2 and name in body and name not in seen:
            seen.add(name)
            names.append(name)
            matched.append(name)
            pids.append(int(row["pid"]))

    # 关系词：他说「我表哥」时，关系表里 a=他、relation=表哥 → 另一头 b
    try:
        with lock:
            rels = conn.execute(
                "SELECT a_uid, b_uid, relation FROM relations WHERE bot_id=?",
                (str(bot_id),),
            ).fetchall()
            by_uid = {
                str(row["uid"]): int(row["pid"])
                for row in conn.execute(
                    "SELECT uid, pid FROM person_links WHERE bot_id=?", (str(bot_id),)
                ).fetchall()
            }
    except Exception:
        rels = []
        by_uid = {}
    for row in rels:
        relation = str(row["relation"] or "").strip()
        if len(relation) < 2 or relation not in body:
            continue
        other = str(row["b_uid"]) if str(row["a_uid"]) == str(uid) else str(row["a_uid"])
        pid = by_uid.get(other)
        if pid and pid not in pids:
            pids.append(int(pid))
            matched.append(relation)
    return {"names": names, "pids": pids[:max(1, int(limit))], "matched": matched[:8]}


def fetch_person_memories(conn, lock, bot_id: str, pids: List[int], scope: str,
                          group_id: str, limit: int = 10,
                          names: Optional[List[str]] = None) -> List[Any]:
    """按 pid 或 `entity:<名字>` 标签取这个人的记忆；scope/group 过滤和文本路一致。"""
    conditions: List[str] = []
    params: List[Any] = [str(bot_id)]
    if pids:
        conditions.append("pid IN (" + ",".join("?" for _ in pids) + ")")
        params.extend(int(pid) for pid in pids)
    for name in (names or [])[:5]:
        token = "entity:" + str(name).strip()
        if len(token) <= len("entity:"):
            continue
        conditions.append("(' ' || COALESCE(tags,'') || ' ') LIKE ?")
        params.append("% " + token + " %")
    if not conditions:
        return []
    sql = ("SELECT id, content, kind, subject, importance, created_at, last_hit_at, "
           "hit_count, scope, group_id, pid, platform, COALESCE(tags,'') AS tags "
           "FROM memories WHERE bot_id=? AND COALESCE(status,'active')='active' "
           "AND (" + " OR ".join(conditions) + ")")
    if scope in ("private", "group"):
        sql += " AND scope=?"
        params.append(scope)
    if group_id:
        sql += " AND group_id=?"
        params.append(str(group_id))
    sql += " ORDER BY importance DESC, last_hit_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    try:
        with lock:
            return conn.execute(sql, params).fetchall()
    except Exception:
        return []
