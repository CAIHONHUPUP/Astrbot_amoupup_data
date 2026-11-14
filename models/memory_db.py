# -*- coding: utf-8 -*-
"""models/memory_db.py —— SQLite 连接、建表、补列、索引与纯 SQL 回填。

原 store.py（连接段）+ store_schema.py（建表/迁移段）的落地位置。

边界：
* 只碰 SQL 结构，不碰检索打分 / 去重判定 / 人物合并；
* 人物自愈（repair_persons / adopt_orphan_memories / merge_ghost_persons）、
  身份归一（normalize_identities）、记忆来源回填（backfill_memory_platform）
  属于 services，装配完成后单独调用（见 ARCHITECTURE 阶段 4）；
* 结构迁移里的 pid / subject 回填是纯 SQL、可重复执行，留在这里。

兼容：库路径、表名、列名、索引名、idx_mem_idem 唯一索引与线上 3.0.0 完全一致，
老库直接打开即可，无需重建。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Set

try:  # 线上走 AstrBot 日志
    from astrbot.api import logger
except Exception:  # pragma: no cover - 离线单测
    class _NullLogger:
        def __getattr__(self, name):
            def _noop(*args, **kwargs):
                return None
            return _noop

    logger = _NullLogger()

DEFAULT_DB_SETTINGS: Dict[str, Any] = {
    # trigram FTS 子串粗筛：默认关，打开后才建 memories_ftst
    "recall_fts_enabled": False,
}


class MemoryDB:
    """一个连接 + 一把 RLock + 全部结构迁移。

    上层（services/*）通过 db.conn / db.lock 做读写；不要在别处再开连接，
    否则 WAL 下的写锁会互相打架。
    """

    def __init__(self, db_path: str, settings: Optional[Dict[str, Any]] = None):
        self.path = str(db_path)
        self.lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self.settings: Dict[str, Any] = dict(DEFAULT_DB_SETTINGS)
        if settings:
            self.settings.update({k: v for k, v in settings.items() if v is not None})
        self.fts_trigram = False
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._open()
        self._init_schema()
        self._migrate_columns()
        self._init_trigram_fts()

    # ---------- 连接 ----------
    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, DEFAULT_DB_SETTINGS.get(key, default))

    def _open(self) -> None:
        with self.lock:
            self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
            self._conn.row_factory = sqlite3.Row
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            with self.lock:
                if self._conn is None:
                    self._open()
        assert self._conn is not None
        return self._conn

    def close(self) -> None:
        with self.lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ---------- 结构 ----------
    def _init_schema(self) -> None:
        with self.lock:
            conn = self.conn
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    bot_id TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    alias TEXT DEFAULT '',
                    relation TEXT DEFAULT '',
                    remark TEXT DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (bot_id, uid)
                );

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'private',
                    group_id TEXT DEFAULT '',
                    kind TEXT DEFAULT 'fact',
                    subject TEXT DEFAULT 'user',
                    content TEXT NOT NULL,
                    importance REAL DEFAULT 0.5,
                    hit_count INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    last_hit_at INTEGER NOT NULL,
                    idem_key TEXT DEFAULT '',
                    status TEXT DEFAULT 'active',
                    superseded_by INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_mem_lookup ON memories(bot_id, uid, scope, group_id);
                CREATE INDEX IF NOT EXISTS idx_mem_hit ON memories(bot_id, uid, last_hit_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_idem
                    ON memories(bot_id, uid, scope, group_id, idem_key)
                    WHERE idem_key != '';

                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content, content='memories', content_rowid='id'
                );
                CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
                    DELETE FROM memories_fts WHERE rowid = old.id;
                END;
                CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE OF content ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content)
                        VALUES('delete', old.id, old.content);
                    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                END;

                CREATE TABLE IF NOT EXISTS memory_vec (
                    memory_id INTEGER PRIMARY KEY,
                    model TEXT DEFAULT '',
                    dim INTEGER DEFAULT 0,
                    vec BLOB,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    umo TEXT NOT NULL,
                    uid TEXT DEFAULT '',
                    name TEXT DEFAULT '',
                    role TEXT DEFAULT 'user',
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    compressed INTEGER DEFAULT 0,
                    message_id TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_msg_scan ON messages(bot_id, umo, created_at);
                -- idx_msg_mid 在 _migrate_columns 里建：老库还没有 message_id 列

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    level TEXT DEFAULT 'info',
                    tag TEXT DEFAULT '',
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_log_scan ON logs(bot_id, created_at);

                CREATE TABLE IF NOT EXISTS relations (
                    bot_id TEXT NOT NULL,
                    a_uid TEXT NOT NULL,
                    b_uid TEXT NOT NULL,
                    relation TEXT DEFAULT '朋友',
                    note TEXT DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (bot_id, a_uid, b_uid)
                );

                CREATE TABLE IF NOT EXISTS cache (
                    bot_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    expires_at INTEGER DEFAULT 0,
                    PRIMARY KEY (bot_id, key)
                );

                CREATE TABLE IF NOT EXISTS persons (
                    pid INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL,
                    name TEXT DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_person_bot ON persons(bot_id, updated_at);

                CREATE TABLE IF NOT EXISTS person_links (
                    bot_id TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT '',
                    uid TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    name TEXT DEFAULT '',
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (bot_id, platform, uid)
                );
                CREATE INDEX IF NOT EXISTS idx_link_pid ON person_links(bot_id, pid);

                CREATE TABLE IF NOT EXISTS identity_alias (
                    bot_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    alias_platform TEXT NOT NULL,
                    alias_uid TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (bot_id, platform, uid)
                );

                -- 一次性迁移标记（原来只在开 trigram 时建，这里无条件建，标记才可靠）
                CREATE TABLE IF NOT EXISTS store_meta(
                    key TEXT PRIMARY KEY, value TEXT DEFAULT ''
                );
                """
            )
            conn.commit()
            # 2026-11-14：旧版 mem_au 直接 UPDATE 外部内容 FTS，特定中文会报
            # "database disk image is malformed"；改成 FTS5 标准的 delete+insert，
            # 并重建一次索引修复老库里可能已损坏的 FTS 数据。
            try:
                with self.lock:
                    conn = self.conn
                    conn.executescript(
                        """
                        DROP TRIGGER IF EXISTS mem_au;
                        CREATE TRIGGER mem_au AFTER UPDATE OF content ON memories BEGIN
                            INSERT INTO memories_fts(memories_fts, rowid, content)
                                VALUES('delete', old.id, old.content);
                            INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                        END;
                        """
                    )
                    marker = conn.execute(
                        "SELECT value FROM store_meta WHERE key='fts_rebuild_v2'"
                    ).fetchone()
                    if not marker:
                        conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
                        conn.execute(
                            "INSERT OR REPLACE INTO store_meta(key, value) VALUES('fts_rebuild_v2', '1')"
                        )
                    conn.commit()
            except Exception:
                pass

    def _init_trigram_fts(self) -> None:
        """建 trigram FTS5 表做子串粗筛；SQLite 不支持时自动停用。

        memories_fts（unicode61）对中文没用——整段中文会被当成一个 token，
        所以 trigram 才能按「>=3 个字」做子串匹配，替代大表上的
        content LIKE '%查询%' 全表扫；2 个字以下的查询仍走 LIKE。
        """
        self.fts_trigram = False
        if not self.setting("recall_fts_enabled", False):
            return
        try:
            with self.lock:
                conn = self.conn
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memories_ftst USING fts5("
                    "content, content='memories', content_rowid='id', tokenize='trigram')"
                )
                conn.executescript(
                    """
                    CREATE TRIGGER IF NOT EXISTS memt_ai AFTER INSERT ON memories BEGIN
                        INSERT INTO memories_ftst(rowid, content) VALUES (new.id, new.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memt_ad AFTER DELETE ON memories BEGIN
                        DELETE FROM memories_ftst WHERE rowid = old.id;
                    END;
                    CREATE TRIGGER IF NOT EXISTS memt_au AFTER UPDATE OF content ON memories BEGIN
                        INSERT INTO memories_ftst(memories_ftst, rowid, content)
                            VALUES('delete', old.id, old.content);
                        INSERT INTO memories_ftst(rowid, content) VALUES (new.id, new.content);
                    END;
                    """
                )
                # 影子表的行数不等于「建了索引的文档数」（trigram 表有固定头部行），
                # 所以用 store_meta 记「上次 rebuild 时 memories 有多少条」。
                total = int(conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"])
                row = conn.execute(
                    "SELECT value FROM store_meta WHERE key='fts_tri_rows'"
                ).fetchone()
                try:
                    built = int(row["value"]) if row else -1
                except (TypeError, ValueError):
                    built = -1
                rebuilt = built != total
                if rebuilt:
                    conn.execute("INSERT INTO memories_ftst(memories_ftst) VALUES('rebuild')")
                    conn.execute(
                        "INSERT INTO store_meta(key, value) VALUES('fts_tri_rows', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(total),),
                    )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mem_recall ON memories("
                    "bot_id, uid, status, last_hit_at)"
                )
                conn.commit()
                self.fts_trigram = True
                if rebuilt:
                    logger.info("[amo_data] trigram 索引已重建：%d 条记忆" % total)
        except Exception as exc:
            self.fts_trigram = False
            logger.warning("[amo_data] trigram 索引不可用，召回退回 LIKE 全表扫: %s" % exc)

    # ---------- 迁移 ----------
    def _table_columns(self, table: str) -> Set[str]:
        try:
            rows = self.conn.execute("PRAGMA table_info(%s)" % table).fetchall()
        except Exception:
            return set()
        return {str(row["name"]) for row in rows}

    def _migrate_columns(self) -> None:
        """老库补列 + 纯 SQL 回填 + 去重索引升级（可重复执行）。"""
        with self.lock:
            conn = self.conn
            cols = self._table_columns("messages")
            for column, ddl in (
                ("platform", "ALTER TABLE messages ADD COLUMN platform TEXT DEFAULT ''"),
                ("group_name", "ALTER TABLE messages ADD COLUMN group_name TEXT DEFAULT ''"),
                ("pid", "ALTER TABLE messages ADD COLUMN pid INTEGER DEFAULT 0"),
                ("message_id", "ALTER TABLE messages ADD COLUMN message_id TEXT DEFAULT ''"),
            ):
                if column in cols:
                    continue
                try:
                    conn.execute(ddl)
                except Exception as exc:
                    logger.warning("[amo_data] 补列 %s 失败: %s" % (column, exc))
                    continue
            try:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_pid ON messages(bot_id, pid, created_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_mid ON messages(bot_id, message_id)")
            except Exception:
                pass

            # 记忆表补列：近重复合并计数、分层状态、来源、人物主键
            mcols = self._table_columns("memories")
            for column, ddl in (
                ("merged_count", "ALTER TABLE memories ADD COLUMN merged_count INTEGER DEFAULT 1"),
                ("status", "ALTER TABLE memories ADD COLUMN status TEXT DEFAULT 'active'"),
                ("superseded_by", "ALTER TABLE memories ADD COLUMN superseded_by INTEGER DEFAULT 0"),
                ("subject", "ALTER TABLE memories ADD COLUMN subject TEXT DEFAULT 'user'"),
                ("pid", "ALTER TABLE memories ADD COLUMN pid INTEGER DEFAULT 0"),
                ("platform", "ALTER TABLE memories ADD COLUMN platform TEXT DEFAULT ''"),
            ):
                if column in mcols:
                    continue
                try:
                    conn.execute(ddl)
                except Exception:
                    pass

            # 历史回填：先补消息 pid（她自己的发言行 umo 尾部就是对方 uid），
            # 再按 uid 把记忆挂到人物上；都只动 pid 仍为 0 的行。
            try:
                conn.execute(
                    "UPDATE messages SET pid = COALESCE((SELECT l.pid FROM person_links l "
                    "WHERE l.bot_id = messages.bot_id "
                    "AND l.uid = substr(messages.umo, instr(messages.umo, ':FriendMessage:') + 15) "
                    "LIMIT 1), 0) "
                    "WHERE COALESCE(pid,0)=0 AND instr(umo, ':FriendMessage:') > 0 "
                    "AND substr(messages.umo, instr(messages.umo, ':FriendMessage:') + 15) <> ''"
                )
                conn.execute(
                    "UPDATE memories SET pid = COALESCE((SELECT l.pid FROM person_links l "
                    "WHERE l.bot_id = memories.bot_id AND l.uid = memories.uid LIMIT 1), 0) "
                    "WHERE COALESCE(pid,0)=0"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mem_pid ON memories(bot_id, pid, scope, group_id)"
                )
            except Exception:
                pass

            # 旧版把 bot 自己发的照片/语音写在用户 uid 下，subject 补成 bot。
            try:
                conn.execute(
                    "UPDATE memories SET subject='bot' WHERE subject='user' "
                    "AND (content LIKE '[我发的照片]%' OR content LIKE '[我发的语音]%')"
                )
            except Exception:
                pass

            # 去重索引要跟着 scope 走：老库是 (bot_id, uid, idem_key)，
            # 同句在群聊和私聊各说一次会撞 UNIQUE。
            try:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_mem_idem'"
                ).fetchone()
                sql = str(row["sql"] or "") if row else ""
                if row is None or "scope" not in sql:
                    conn.execute("DROP INDEX IF EXISTS idx_mem_idem")
                    conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_idem "
                        "ON memories(bot_id, uid, scope, group_id, idem_key) "
                        "WHERE idem_key != ''"
                    )
                    logger.info("[amo_data] 记忆去重索引已升级为按会话隔离")
                conn.commit()
            except Exception as exc:
                # 建不出来（例如库里有遗留重复 idem_key）也不影响用：
                # remember 里的查询照样按 (scope, group_id) 兜着
                logger.warning("[amo_data] 记忆去重索引升级失败: %s" % exc)

    # ---------- 只读辅助 ----------
    def bot_ids(self) -> List[str]:
        """面板/维护用：列出库里出现过的所有 bot_id。"""
        ids = set()
        with self.lock:
            for table in ("profiles", "memories", "messages", "logs", "relations", "cache", "persons"):
                try:
                    for row in self.conn.execute(
                        "SELECT DISTINCT bot_id FROM %s WHERE bot_id != ''" % table
                    ).fetchall():
                        value = str(row["bot_id"] or "").strip()
                        if value:
                            ids.add(value)
                except Exception:
                    continue
        return sorted(ids)
