# -*- coding: utf-8 -*-
"""services/cache_engine.py —— 带 TTL 的 KV 缓存（备份日标记 / 每周整理标记等）。

表结构与语义沿用 3.0：ttl>0 到点过期，ttl<0 立即过期，ttl=0 永不过期；值按 JSON 存。
"""

from __future__ import annotations

import json
from typing import Any, List

try:
    from ..utils.text_process import _bot_filter, _now, logger
except ImportError:  # 顶层导入（测试/脚本）
    from utils.text_process import _bot_filter, _now, logger


class CacheEngine:
    def __init__(self, db):
        self.db = db

    @property
    def conn(self):
        return self.db.conn

    @property
    def lock(self):
        return self.db.lock

    def cache_get(self, bot_id: str, key: str) -> Any:
        with self.lock:
            row = self.conn.execute(
                "SELECT value, expires_at FROM cache WHERE bot_id=? AND key=?",
                (str(bot_id), str(key)),
            ).fetchone()
        if not row:
            return None
        if row["expires_at"] and int(row["expires_at"]) < _now():
            self.cache_delete(bot_id, key)
            return None
        try:
            return json.loads(row["value"])
        except Exception:
            return None

    def cache_set(self, bot_id: str, key: str, value: Any, ttl: int = 0) -> bool:
        try:
            payload = json.dumps(value, ensure_ascii=False)
        except Exception:
            return False
        # ttl>0 正常过期；ttl<0 表示一写入就该过期；ttl=0 表示永不过期
        expires = _now() + int(ttl) if ttl else 0
        with self.lock:
            self.conn.execute(
                "INSERT INTO cache(bot_id, key, value, updated_at, expires_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(bot_id, key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at, expires_at=excluded.expires_at",
                (str(bot_id), str(key), payload, _now(), expires),
            )
            self.conn.commit()
        return True

    def cache_delete(self, bot_id: str, key: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM cache WHERE bot_id=? AND key=?", (str(bot_id), str(key))
            )
            self.conn.commit()
            return bool(cur.rowcount)

    def cache_keys(self, bot_id: str = "") -> List[str]:
        where, params = _bot_filter(bot_id)
        with self.lock:
            rows = self.conn.execute(
                "SELECT key FROM cache WHERE 1=1" + where + " ORDER BY updated_at DESC", params
            ).fetchall()
        return [str(row["key"]) for row in rows]
