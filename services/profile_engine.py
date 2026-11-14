# -*- coding: utf-8 -*-
"""services/profile_engine.py —— 画像 CRUD（名字/称呼/关系/备注）。"""

from __future__ import annotations

import sqlite3


from typing import Any, Dict, List, Optional

try:  # AstrBot 包模式
    from ..utils.text_process import (
        _bot_filter,
        _clean,
        _now,
    )
except ImportError:  # 顶层导入（测试/脚本）
    from utils.text_process import (
        _bot_filter,
        _clean,
        _now,
    )


class ProfileEngine:
    """画像读写与删除；身份归一交给 PersonEngine。"""

    def __init__(self, db, settings=None, persons=None):
        self.db = db
        self.settings = settings if settings is not None else {}
        self.persons = persons
        self.plugin = None
        self._canonical_cache = {}

    # ---- 迁移期适配：老 mixin 里直接用了 self.conn / self.lock / self.setting ----
    @property
    def conn(self) -> sqlite3.Connection:
        return self.db.conn

    @property
    def lock(self):
        return self.db.lock

    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    # 中文时间词（原 DataStore 上的类属性，聊天时间窗解析要用）
    _CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
               "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    _PERIODS = (
        (("凌晨",), 0, 6),
        (("早上", "早晨", "上午"), 6, 12),
        (("中午",), 11, 14),
        (("下午",), 12, 18),
        (("傍晚", "晚上", "今晚", "昨晚", "夜里", "深夜"), 18, 24),
    )

    def get_profile(self, bot_id: str, uid: str) -> Dict[str, Any]:
        with self.lock:
            row = self.conn.execute(
                "SELECT name, alias, relation, remark, updated_at FROM profiles "
                "WHERE bot_id=? AND uid=?",
                (str(bot_id), str(uid)),
            ).fetchone()
        if not row:
            return {
                "uid": str(uid), "name": "", "alias": "", "relation": "",
                "remark": "", "updated_at": 0, "exists": False,
            }
        return {
            "uid": str(uid),
            "name": str(row["name"] or ""),
            "alias": str(row["alias"] or ""),
            "relation": str(row["relation"] or ""),
            "remark": str(row["remark"] or ""),
            "updated_at": int(row["updated_at"] or 0),
            "exists": True,
        }


    def find_profile(self, uid: str) -> Dict[str, Any]:
        """跨 bot 找画像：面板默认聚合所有 bot，编辑时要先知道这条属于哪个 bot。"""
        uid = str(uid)
        if not uid:
            return {}
        with self.lock:
            row = self.conn.execute(
                "SELECT bot_id FROM profiles WHERE uid=? ORDER BY updated_at DESC LIMIT 1",
                (uid,),
            ).fetchone()
        if not row:
            return {}
        bot_id = str(row["bot_id"] or "")
        profile = self.get_profile(bot_id, uid)
        profile["bot_id"] = bot_id
        return profile


    def save_profile(
        self,
        bot_id: str,
        uid: str,
        name: Optional[str] = None,
        alias: Optional[str] = None,
        relation: Optional[str] = None,
        remark: Optional[str] = None,
    ) -> Dict[str, Any]:
        """只更新传进来的字段，没传的保持原值；整条不存在就新建。"""
        bot_id, uid = str(bot_id), str(uid)
        with self.lock:
            current = self.get_profile(bot_id, uid)
            merged = {
                "name": current["name"] if name is None else _clean(name, 64),
                "alias": current["alias"] if alias is None else _clean(alias, 128),
                "relation": current["relation"] if relation is None else _clean(relation, 32),
                "remark": current["remark"] if remark is None else _clean(remark, 500),
            }
            self.conn.execute(
                "INSERT INTO profiles(bot_id, uid, name, alias, relation, remark, updated_at) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(bot_id, uid) DO UPDATE SET "
                "name=excluded.name, alias=excluded.alias, relation=excluded.relation, "
                "remark=excluded.remark, updated_at=excluded.updated_at",
                (bot_id, uid, merged["name"], merged["alias"], merged["relation"],
                 merged["remark"], _now()),
            )
            # 画像改名后，人物卡的名字也要跟着改：否则会一直显示最早建档时的旧名字
            if merged["name"]:
                self.conn.execute(
                    "UPDATE persons SET name=? WHERE bot_id=? AND pid IN "
                    "(SELECT pid FROM person_links WHERE bot_id=? AND uid=?)",
                    (merged["name"], bot_id, bot_id, uid),
                )
            self.conn.commit()
        return self.get_profile(bot_id, uid)


    def list_profiles(self, bot_id: str, keyword: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        where, params = _bot_filter(bot_id)
        sql = "SELECT uid, name, alias, relation, remark, updated_at FROM profiles WHERE 1=1" + where
        if keyword:
            sql += " AND (uid LIKE ? OR name LIKE ? OR alias LIKE ? OR remark LIKE ?)"
            like = "%" + str(keyword) + "%"
            params.extend([like, like, like, like])
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


    def delete_profile(self, bot_id: str, uid: str) -> Dict[str, int]:
        """删画像，连带这个人的记忆、关系边和缓存。"""
        bot_id, uid = str(bot_id), str(uid)
        counts: Dict[str, int] = {}
        with self.lock:
            pid = self.persons.pid_for_uid(bot_id, uid)
            for table, sql, params in (
                ("profiles", "DELETE FROM profiles WHERE bot_id=? AND uid=?", (bot_id, uid)),
                ("memories", "DELETE FROM memories WHERE bot_id=? AND (uid=? OR pid=?)",
                 (bot_id, uid, int(pid))),
                ("relations", "DELETE FROM relations WHERE bot_id=? AND (a_uid=? OR b_uid=?)",
                 (bot_id, uid, uid)),
            ):
                cur = self.conn.execute(sql, params)
                counts[table] = max(0, cur.rowcount or 0)
            self.conn.commit()
        return counts

