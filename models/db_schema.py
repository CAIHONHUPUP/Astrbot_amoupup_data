# -*- coding: utf-8 -*-
"""models/db_schema.py —— DB schema 版本：PRAGMA user_version + 迁移注册表（Phase 12）。

为什么要有这一层
- 3.3.0 之前，结构升级散在 MemoryDB._init_schema / _migrate_columns /
  _repair_fts_trigger 和 store_meta 的 fts_rebuild_v2 标记里：谁跑过、跑到第几步
  启动日志看不出来，失败了也不留痕；
- 现在用 SQLite 自带的 PRAGMA user_version 当版本号（不新增表/列/配置键），
  迁移登记成 (version, id, fn)：按序执行、幂等、只前进不回退；
- 单项失败记 last_error、停在原地，由 MemoryDB.__init__ 兜住异常，不阻断启动；
- 库版本比代码高（回滚过）只告警，不降级、不重跑。

约定
- fn(db) 收 MemoryDB 实例（用 db.conn / db.lock）；建表仍由 memory_db.SCHEMA_SQL 负责；
- 迁移项必须可重入：失败后下次启动从头重跑同一项也要安全；
- 不改表名/列名、不删数据；补列只做 ALTER TABLE ADD COLUMN；
- store_meta 里的 fts_rebuild_v2 标记保留（老库靠它跳过重复 rebuild）；
  fts_tri_rows 不是版本号，而是 trigram 索引「上次建索引时 memories 有多少行」的
  行数快照，由 memory_db._init_trigram_fts 每次启动按行数决定是否重建，不进版本表。
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # 线上走 AstrBot 日志
    from astrbot.api import logger
except Exception:  # pragma: no cover - 离线单测
    import logging

    logger = logging.getLogger("amo_data")

#: 代码期望的最新 schema 版本（只在加迁移项时 +1）
LATEST_VERSION = 3

_LOCK = threading.RLock()
_LAST_ERROR: Optional[str] = None

#: FTS 更新触发器修复 SQL（2026-11-14 malformed 那个修复；v3 迁移重放一遍）
FTS_TRIGGER_REPAIR_SQL = """
DROP TRIGGER IF EXISTS mem_au;
CREATE TRIGGER mem_au AFTER UPDATE OF content ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content)
        VALUES('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

#: messages 老库补列（列名 / DDL）
MESSAGE_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("platform", "ALTER TABLE messages ADD COLUMN platform TEXT DEFAULT ''"),
    ("group_name", "ALTER TABLE messages ADD COLUMN group_name TEXT DEFAULT ''"),
    ("pid", "ALTER TABLE messages ADD COLUMN pid INTEGER DEFAULT 0"),
    ("message_id", "ALTER TABLE messages ADD COLUMN message_id TEXT DEFAULT ''"),
)

#: memories 老库补列（列名 / DDL）
MEMORY_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("merged_count", "ALTER TABLE memories ADD COLUMN merged_count INTEGER DEFAULT 1"),
    ("status", "ALTER TABLE memories ADD COLUMN status TEXT DEFAULT 'active'"),
    ("superseded_by", "ALTER TABLE memories ADD COLUMN superseded_by INTEGER DEFAULT 0"),
    ("subject", "ALTER TABLE memories ADD COLUMN subject TEXT DEFAULT 'user'"),
    ("pid", "ALTER TABLE memories ADD COLUMN pid INTEGER DEFAULT 0"),
    ("platform", "ALTER TABLE memories ADD COLUMN platform TEXT DEFAULT ''"),
    ("tags", "ALTER TABLE memories ADD COLUMN tags TEXT DEFAULT ''"),
)


def _migration_baseline(db) -> int:
    """v1 基线：建表（SCHEMA_SQL）已在 MemoryDB._init_schema 里做完，这里只登记版本。"""
    return 0


def _table_columns(conn, table: str) -> List[str]:
    """读表列名；读不到（表不存在/只读库）返回空列表，由补列阶段再报。"""
    try:
        rows = conn.execute("PRAGMA table_info(%s)" % table).fetchall()
    except Exception as exc:
        logger.debug("[amo_data] 读 %s 表结构失败: %r", table, exc)
        return []
    return [str(row[1]) for row in rows]


def _add_columns(conn, table: str, pairs: Iterable[Tuple[str, str]]) -> int:
    """补列：已存在就跳过；ALTER 失败向上抛，交给 run() 记 last_error。"""
    existing = set(_table_columns(conn, table))
    added = 0
    for column, ddl in pairs:
        if column in existing:
            continue
        conn.execute(ddl)
        added += 1
    return added


def _backfill_person_ids(conn) -> None:
    """历史回填：消息/记忆的 pid 只补仍为 0 的行（纯 SQL，可重复执行）。"""
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
    # 旧版把 bot 自己发的照片/语音写在用户 uid 下，subject 补成 bot。
    conn.execute(
        "UPDATE memories SET subject='bot' WHERE subject='user' "
        "AND (content LIKE '[我发的照片]%' OR content LIKE '[我发的语音]%')"
    )


def _upgrade_idem_index(conn) -> None:
    """去重索引要跟着 scope 走：老库 (bot_id, uid, idem_key) 会跨会话撞 UNIQUE。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_mem_idem'"
    ).fetchone()
    sql = str(row[0] or "") if row else ""
    if row is not None and "scope" in sql:
        return
    try:
        conn.execute("DROP INDEX IF EXISTS idx_mem_idem")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_idem "
            "ON memories(bot_id, uid, scope, group_id, idem_key) "
            "WHERE idem_key != ''"
        )
        logger.info("[amo_data] 记忆去重索引已升级为按会话隔离")
    except Exception as exc:
        # 建不出来（例如库里有遗留重复 idem_key）也不影响用：
        # remember 里的查询照样按 (scope, group_id) 兜着
        logger.warning("[amo_data] 记忆去重索引升级失败: %s" % exc)


def _migration_legacy_columns(db) -> int:
    """v2：老库补列 + pid 回填 + 去重索引升级（可重复执行）。"""
    with db.lock:
        conn = db.conn
        _add_columns(conn, "messages", MESSAGE_COLUMNS)
        _add_columns(conn, "memories", MEMORY_COLUMNS)
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_pid ON messages(bot_id, pid, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_mid ON messages(bot_id, message_id)")
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)
        _upgrade_idem_index(conn)
        conn.commit()
    return 0


def _migration_fts_rebuild(db) -> int:
    """v3：修复 FTS 更新触发器；store_meta 没有 fts_rebuild_v2 标记时整表 rebuild 一次。"""
    with db.lock:
        conn = db.conn
        conn.executescript(FTS_TRIGGER_REPAIR_SQL)
        marker = conn.execute(
            "SELECT value FROM store_meta WHERE key='fts_rebuild_v2'"
        ).fetchone()
        if not marker:
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            conn.execute(
                "INSERT OR REPLACE INTO store_meta(key, value) VALUES('fts_rebuild_v2', '1')"
            )
        conn.commit()
    return 0



def maintain_data(db) -> None:
    """每次启动都跑的幂等数据维护（不是 schema 版本）：pid 回填 + 旧照片/语音 subject 修正。

    为什么放在版本迁移之外：pid 回填要覆盖「上次启动之后新写入的 pid=0 行」，
    只跑一次会漏（老库升级、导入、断点写入都会产生这种行）。失败只记日志，不阻断启动。
    """
    try:
        with db.lock:
            _backfill_person_ids(db.conn)
            db.conn.commit()
    except Exception as exc:
        logger.warning("[amo_data] 启动数据回填失败（不影响启动）: %s", exc)


#: 迁移注册表：version 递增，(version, id, fn, note)
MIGRATIONS = (
    {
        "version": 1,
        "id": "schema_baseline",
        "fn": _migration_baseline,
        "note": "建表（SCHEMA_SQL）已在 MemoryDB._init_schema 完成",
    },
    {
        "version": 2,
        "id": "legacy_columns",
        "fn": _migration_legacy_columns,
        "note": "messages/memories 补列、pid 回填、idx_mem_idem 按 scope 升级",
    },
    {
        "version": 3,
        "id": "fts_rebuild_v2",
        "fn": _migration_fts_rebuild,
        "note": "修 FTS 更新触发器；兼容 store_meta 的 fts_rebuild_v2 标记",
    },
)


def _conn(db):
    conn = getattr(db, "conn", None)
    if conn is None:
        raise TypeError("schema 迁移需要 MemoryDB 连接（db.conn）")
    return conn


def user_version(db) -> int:
    """读 PRAGMA user_version；读不到按 0（等价于「从没迁移过」）。"""
    try:
        row = _conn(db).execute("PRAGMA user_version").fetchone()
    except Exception as exc:
        logger.debug("[amo_data] 读 user_version 失败: %r", exc)
        return 0
    if not row:
        return 0
    try:
        return int(row[0] or 0)
    except (TypeError, ValueError):
        return 0


def set_user_version(db, version: int) -> int:
    """写 PRAGMA user_version（只前进由 run() 保证）。"""
    value = max(0, int(version))
    conn = _conn(db)
    # PRAGMA 不支持参数绑定；value 已经 int 化，不存在注入面。
    conn.execute("PRAGMA user_version=%d" % value)
    conn.commit()
    return value


def last_error() -> Optional[str]:
    """最近一次迁移失败的原因（没有失败就是 None）。"""
    return _LAST_ERROR


def reset_last_error() -> None:
    """清空 last_error（测试/人工修复后重试用）。"""
    global _LAST_ERROR
    _LAST_ERROR = None


def _registry(migrations) -> Tuple[Dict[str, Any], ...]:
    return tuple(MIGRATIONS if migrations is None else migrations)


def pending(db, migrations=None) -> List[Dict[str, Any]]:
    """还没应用的迁移项（按 version 升序）。"""
    current = user_version(db)
    return [dict(item) for item in _registry(migrations) if int(item["version"]) > current]


def status(db, migrations=None) -> Dict[str, Any]:
    """schema 快照：{user_version, latest_version, applied, pending, last_error, ahead}。

    只读：不建表、不跑迁移、不写版本号。
    """
    items = _registry(migrations)
    try:
        current = user_version(db)
    except Exception as exc:
        return {
            "user_version": 0,
            "latest_version": LATEST_VERSION,
            "applied": [],
            "pending": [],
            "last_error": "%s: %s" % (type(exc).__name__, exc),
            "ahead": False,
        }
    return {
        "user_version": current,
        "latest_version": LATEST_VERSION,
        "applied": [item["id"] for item in items if int(item["version"]) <= current],
        "pending": [item["id"] for item in items if int(item["version"]) > current],
        "last_error": _LAST_ERROR,
        "ahead": current > LATEST_VERSION,
    }


def run(db, migrations=None) -> Dict[str, Any]:
    """按序跑未应用的迁移，返回 status() + applied_now。

    - 只前进：某项成功后写 user_version=该项 version；
    - 幂等：user_version 已覆盖的项直接跳过；
    - 失败不抛：回滚未提交的 DDL、记 last_error、停在该项之前，不阻断启动。
    """
    global _LAST_ERROR
    with _LOCK:
        before = status(db, migrations)
        if before["ahead"]:
            logger.warning(
                "[amo_data] DB schema user_version=%d 高于代码期望的 %d，不降级，只告警",
                before["user_version"], LATEST_VERSION,
            )
            before["applied_now"] = []
            return before
        current = before["user_version"]
        applied_now: List[str] = []
        for item in sorted(_registry(migrations), key=lambda entry: int(entry["version"])):
            version = int(item["version"])
            if version <= current:
                continue
            fn = item.get("fn")
            if callable(fn):
                try:
                    fn(db)
                except Exception as exc:
                    _LAST_ERROR = "%s: %s: %s" % (item["id"], type(exc).__name__, exc)
                    logger.warning(
                        "[amo_data] DB 迁移 %s（v%d）失败，停在 v%d：%s",
                        item["id"], version, current, exc,
                    )
                    _rollback(db)
                    break
            try:
                set_user_version(db, version)
            except Exception as exc:
                _LAST_ERROR = "%s: 写 user_version 失败: %s" % (item["id"], exc)
                logger.warning("[amo_data] DB 迁移 %s 写版本号失败：%s", item["id"], exc)
                _rollback(db)
                break
            current = version
            applied_now.append(item["id"])
        after = status(db, migrations)
        after["applied_now"] = applied_now
        return after


def _rollback(db) -> None:
    """迁移失败时回滚未提交的 DDL，保证下次启动重跑同一项仍幂等。"""
    try:
        db.conn.rollback()
    except Exception as exc:
        logger.debug("[amo_data] 迁移失败后回滚出错: %r", exc)
