# -*- coding: utf-8 -*-
"""services/log_engine.py —— 运行日志（面板日志页 + 到期清理）。"""

from __future__ import annotations

import sqlite3


from typing import Any, Dict, List

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


class LogEngine:
    """只做日志表读写，不含业务判断。"""

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

    def add_log(self, bot_id: str, level: str, tag: str, content: str) -> int:
        text = _clean(content, 2000)
        if not text:
            return 0
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO logs(bot_id, level, tag, content, created_at) VALUES(?,?,?,?,?)",
                (str(bot_id), _clean(level, 16) or "info", _clean(tag, 32), text, _now()),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)


    def list_logs(self, bot_id: str, level: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        where, params = _bot_filter(bot_id)
        sql = "SELECT id, level, tag, content, created_at FROM logs WHERE 1=1" + where
        if level:
            sql += " AND level=?"
            params.append(str(level))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


    def delete_log(self, bot_id: str, log_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM logs WHERE bot_id=? AND id=?", (str(bot_id), int(log_id))
            )
            self.conn.commit()
            return bool(cur.rowcount)


    def clear_logs(self, bot_id: str) -> int:
        with self.lock:
            cur = self.conn.execute("DELETE FROM logs WHERE bot_id=?", (str(bot_id),))
            self.conn.commit()
            return max(0, cur.rowcount or 0)


    def purge_logs(self, bot_id: str, days: int = 7) -> int:
        cutoff = _now() - max(1, int(days)) * 86400
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM logs WHERE bot_id=? AND created_at<?", (str(bot_id), cutoff)
            )
            self.conn.commit()
            return max(0, cur.rowcount or 0)

