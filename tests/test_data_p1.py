# -*- coding: utf-8 -*-
"""Phase 12（能记 P1）：schema_version / person 三层模型 / memory 单一校验路径。

覆盖三组：
1. PRAGMA user_version 迁移注册表：新库/老库/未来版本/失败记录/幂等/只读状态；
2. person：persons + person_links + identity_alias 三层不变式，merge/unlink/adopt/
   ghost/cache 失效边界；
3. memory：写入前统一校验、idem 冲突取最新正文、去重键按 scope 隔离、标签不丢。

反向验证（手工改坏 → 本文件红 → 恢复 → 绿）：
- 迁移跳过：把 db_schema.run() 的 `if version <= current: continue` 删掉 →
  test_reopen_is_idempotent / test_only_pending_migrations_run 红；
- merge 不搬 alias/链接：删掉 merge_persons 里的 person_links UPDATE →
  test_merge_moves_links_messages_memories_and_alias 红；
- 去重键漏 scope：dao.find_active_by_idem 的 SQL 去掉 scope/group_id →
  test_idem_isolated_by_scope 红；
- idem 丢最新内容：_try_merge 非静态分支改回 touch-only →
  test_nonstatic_idem_keeps_latest_content 红。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from models import db_schema  # noqa: E402
from models.memory_db import MemoryDB  # noqa: E402
from services import memory_schema, person_model  # noqa: E402
from services.memory_engine import MemoryEngine  # noqa: E402
from services.person_engine import PersonEngine  # noqa: E402

# ---- 临时目录兜底清理 ------------------------------------------------------
# pytest 用 tests/conftest.py 的 .test-tmp，gate/run_tests.py 用插件根的 _test_tmp；
# 两边都只负责创建、不负责删除。这里在进程退出时按「导入时快照」删掉本次新增的
# 目录/文件，并移除已经空的根，插件根不再残留临时库（只动测试产物，不碰生产代码）。
_TMP_ROOTS = (".test-tmp", "_test_tmp")


def _tmp_snapshot():
    snapshots = {}
    for name in _TMP_ROOTS:
        path = os.path.join(ROOT, name)
        snapshots[path] = set(os.listdir(path)) if os.path.isdir(path) else None
    return snapshots


def _tmp_cleanup():
    for path, before in _TMP_SNAPSHOT.items():
        if not os.path.isdir(path):
            continue
        for name in os.listdir(path):
            if before is not None and name in before:
                continue
            target = os.path.join(path, name)
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    os.unlink(target)
                except OSError:
                    continue
        try:
            if not os.listdir(path):
                os.rmdir(path)
        except OSError:
            continue


_TMP_SNAPSHOT = _tmp_snapshot()
atexit.register(_tmp_cleanup)

BOT = "bot1"
QQ = "10001"
LARK = "ou_parent"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base, prefix="amo-data-p1-")


class _SchemaCase(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.path = os.path.join(self.tmp, "amo.db")
        db_schema.reset_last_error()

    def tearDown(self):
        db_schema.reset_last_error()

    def _open(self) -> MemoryDB:
        db = MemoryDB(self.path)
        self.addCleanup(db.close)
        return db


class SchemaVersionTests(_SchemaCase):
    def test_fresh_db_is_latest_with_all_applied(self):
        db = self._open()
        status = db.schema_status()
        self.assertEqual(db_schema.LATEST_VERSION, status["user_version"])
        self.assertEqual([], status["pending"])
        self.assertEqual(["schema_baseline", "legacy_columns", "fts_rebuild_v2"],
                         status["applied"])
        self.assertIsNone(status["last_error"])
        self.assertFalse(status["ahead"])

    def test_reopen_is_idempotent(self):
        db = self._open()
        first = db_schema.run(db)
        self.assertEqual([], first["applied_now"])
        db.close()
        again = self._open()
        report = db_schema.run(again)
        self.assertEqual([], report["applied_now"])
        self.assertEqual(db_schema.LATEST_VERSION, report["user_version"])

    def test_legacy_db_upgrade_keeps_data_and_adds_columns(self):
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL, umo TEXT NOT NULL, uid TEXT DEFAULT '',
                    name TEXT DEFAULT '', role TEXT DEFAULT 'user',
                    content TEXT NOT NULL, created_at INTEGER NOT NULL,
                    compressed INTEGER DEFAULT 0
                );
                CREATE TABLE memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id TEXT NOT NULL, uid TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'private', group_id TEXT DEFAULT '',
                    kind TEXT DEFAULT 'fact', content TEXT NOT NULL,
                    importance REAL DEFAULT 0.5, hit_count INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL, last_hit_at INTEGER NOT NULL,
                    idem_key TEXT DEFAULT ''
                );
                INSERT INTO messages(bot_id, umo, uid, content, created_at)
                    VALUES('bot1', 'bot1:FriendMessage:10001', '10001', '老消息', 1700000000);
                INSERT INTO memories(bot_id, uid, content, created_at, last_hit_at, idem_key)
                    VALUES('bot1', '10001', '老记忆：他喜欢猫', 1700000000, 1700000000, 'fact:old');
                """
            )
        db = self._open()
        self.assertEqual(db_schema.LATEST_VERSION, db_schema.user_version(db))
        tables = {str(row[0]) for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for name in ("profiles", "memories", "messages", "logs", "relations", "cache",
                     "persons", "person_links", "identity_alias", "store_meta"):
            self.assertIn(name, tables)
        memory_cols = {str(row[1]) for row in db.conn.execute("PRAGMA table_info(memories)")}
        message_cols = {str(row[1]) for row in db.conn.execute("PRAGMA table_info(messages)")}
        self.assertTrue({"tags", "status", "pid", "platform", "subject"} <= memory_cols)
        self.assertTrue({"platform", "pid", "message_id", "group_name"} <= message_cols)
        row = db.conn.execute("SELECT content FROM memories").fetchone()
        self.assertEqual("老记忆：他喜欢猫", str(row["content"]))
        row = db.conn.execute("SELECT content FROM messages").fetchone()
        self.assertEqual("老消息", str(row["content"]))
        self.assertEqual(1, int(db.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]))
        index_sql = str(db.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_mem_idem'").fetchone()[0])
        self.assertIn("scope", index_sql)

    def test_future_version_is_not_downgraded(self):
        db = self._open()
        db_schema.set_user_version(db, 99)
        report = db_schema.run(db)
        self.assertEqual(99, report["user_version"])
        self.assertTrue(report["ahead"])
        self.assertEqual([], report["applied_now"])
        self.assertEqual([], report["pending"])
        self.assertEqual(99, db_schema.user_version(db))

    def test_migration_failure_records_last_error_and_stops(self):
        db = self._open()
        db_schema.set_user_version(db, 1)
        original = db_schema.MIGRATIONS
        self.addCleanup(setattr, db_schema, "MIGRATIONS", original)

        def boom(instance):
            raise RuntimeError("迁移炸了")

        db_schema.MIGRATIONS = (
            original[0],
            {"version": 2, "id": "boom_migration", "fn": boom, "note": "测试用失败项"},
        )
        report = db_schema.run(db)
        self.assertEqual(1, report["user_version"])
        self.assertEqual([], report["applied_now"])
        self.assertEqual(["boom_migration"], report["pending"])
        self.assertIn("boom_migration", report["last_error"])
        self.assertIn("迁移炸了", report["last_error"])
        self.assertEqual(1, db_schema.user_version(db))

    def test_only_pending_migrations_run(self):
        db = self._open()
        calls = []

        def fake(instance):
            calls.append(1)

        original = db_schema.MIGRATIONS
        self.addCleanup(setattr, db_schema, "MIGRATIONS", original)
        db_schema.MIGRATIONS = (
            original[0],
            {"version": 2, "id": "fake_once", "fn": fake, "note": "计数用"},
        )
        db_schema.set_user_version(db, 1)
        first = db_schema.run(db)
        self.assertEqual(["fake_once"], first["applied_now"])
        self.assertEqual(1, len(calls))
        second = db_schema.run(db)
        self.assertEqual([], second["applied_now"])
        self.assertEqual(1, len(calls), "已应用的迁移不许重跑")

    def test_schema_status_is_read_only(self):
        db = self._open()
        before = db_schema.user_version(db)
        snapshot = db.schema_status()
        self.assertEqual(before, snapshot["user_version"])
        self.assertEqual(before, db_schema.user_version(db))
        self.assertIn("last_error", snapshot)
        self.assertEqual(db_schema.user_version(db), snapshot["user_version"])


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.path = os.path.join(self.tmp, "amo.db")
        self.db = MemoryDB(self.path)
        self.persons = PersonEngine(self.db, {})
        self.memory = MemoryEngine(self.db, {"auto_dedupe": True}, persons=self.persons)
        # person_detail 会读 plugin.profiles；这里给个最小替身，避免把 ProfileEngine 拖进来
        self.persons.plugin = types.SimpleNamespace(
            profiles=types.SimpleNamespace(get_profile=lambda bot, uid: {}),
            memory=self.memory,
        )

    def tearDown(self):
        self.memory.close()
        self.db.close()

    def _link(self, platform: str, uid: str, pid: int = 0, name: str = "") -> int:
        return self.persons.link_identity(BOT, platform, uid, pid, name)

    def _add_message(self, platform: str, uid: str, role: str, content: str,
                     pid: int, ts: int = 1700000000, umo: str = "") -> None:
        umo = umo or "%s:FriendMessage:%s" % (platform, uid)
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, "
                "compressed, message_id, platform, group_name, pid) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (BOT, umo, uid, "n", role, content, ts, 0, "", platform, "", int(pid)),
            )
            self.db.conn.commit()

    def _add_memory(self, uid: str, content: str, pid: int = 0,
                    ts: int = 1700000000, idem: str = "") -> int:
        with self.db.lock:
            cur = self.db.conn.execute(
                "INSERT INTO memories(bot_id, uid, pid, content, importance, created_at, "
                "last_hit_at, idem_key) VALUES(?,?,?,?,?,?,?,?)",
                (BOT, uid, int(pid), content, 0.5, ts, ts, idem),
            )
            self.db.conn.commit()
            return int(cur.lastrowid or 0)

    def _rows(self, sql: str, params=()):
        return [dict(row) for row in self.db.conn.execute(sql, params).fetchall()]


class PersonModelTests(_StoreCase):
    def test_three_layer_invariants_hold(self):
        pid = self._link("aiocqhttp", QQ, 0, "阿墨")
        self._link("lark", LARK, pid, "阿墨")
        self.assertEqual(pid, self.persons.pid_for_uid(BOT, QQ))
        self.assertEqual(LARK, self.persons.canonical_uid(BOT, "aiocqhttp", QQ))
        self.assertEqual([], self.persons.person_invariants(BOT))
        persons = self._rows("SELECT pid, bot_id FROM persons")
        links = self._rows("SELECT bot_id, platform, uid, pid FROM person_links")
        aliases = self._rows("SELECT bot_id, platform, uid, alias_platform, alias_uid "
                             "FROM identity_alias")
        self.assertEqual([], person_model.invariants(persons, links, aliases))

    def test_merge_moves_links_messages_memories_and_alias(self):
        keep = self._link("lark", LARK, 0, "阿墨")
        drop = self._link("qq_official", "2080", 0, "阿墨仔")
        self._add_message("lark", LARK, "user", "飞书这句", keep)
        self._add_message("qq_official", "2080", "user", "QQ 这句", drop)
        self.assertEqual(True, self.memory.remember(
            BOT, "2080", "他喜欢狗", platform="qq_official")["ok"])
        self.persons.set_identity_alias(BOT, "qq", "legacy-uid", "qq_official", "2080")
        self.assertTrue(self.persons.merge_persons(BOT, keep, drop))
        self.assertEqual({}, self.persons.person_detail(BOT, drop))
        self.assertEqual(2, len(self.persons.person_detail(BOT, keep)["links"]))
        pids = {int(row["pid"]) for row in self._rows("SELECT pid FROM messages")}
        self.assertEqual({keep}, pids)
        mems = self._rows("SELECT pid, uid FROM memories")
        self.assertEqual({keep}, {int(row["pid"]) for row in mems})
        self.assertEqual({LARK}, {str(row["uid"]) for row in mems},
                         "合并后记忆 uid 要归一成保留人物的主 uid")
        self.assertEqual(keep, self.persons.person_pid(BOT, "qq", "legacy-uid"),
                         "alias 指向身份，合并后应自动跟随到 keep")
        self.assertEqual([], self.persons.person_invariants(BOT))

    def test_unlink_moves_platform_messages_even_with_stale_uid(self):
        pid = self._link("aiocqhttp", QQ, 0, "阿墨")
        self._link("lark", LARK, pid, "阿墨")
        # 飞书消息的 uid 可能是归一前的老 uid，按 uid 搬会漏
        self._add_message("lark", QQ, "user", "飞书这句", pid)
        self._add_message("aiocqhttp", QQ, "user", "QQ 这句", pid)
        self.assertTrue(self.persons.unlink_identity(BOT, "lark", LARK))
        new_pid = self.persons.person_pid(BOT, "lark", LARK)
        self.assertTrue(new_pid > 0)
        self.assertNotEqual(pid, new_pid)
        lark_msg = self._rows("SELECT pid FROM messages WHERE platform='lark'")
        qq_msg = self._rows("SELECT pid FROM messages WHERE platform='aiocqhttp'")
        self.assertEqual([new_pid], [int(row["pid"]) for row in lark_msg])
        self.assertEqual([pid], [int(row["pid"]) for row in qq_msg])

    def test_unlink_with_multiple_same_platform_links_moves_only_uid(self):
        pid = self._link("aiocqhttp", QQ, 0, "甲")
        self._link("aiocqhttp", "10002", pid, "甲")
        self._add_message("aiocqhttp", QQ, "user", "甲的话", pid)
        self._add_message("aiocqhttp", "10002", "user", "乙的话", pid)
        self.assertTrue(self.persons.unlink_identity(BOT, "aiocqhttp", QQ))
        new_pid = self.persons.person_pid(BOT, "aiocqhttp", QQ)
        moved = self._rows("SELECT uid, pid FROM messages WHERE uid=?", (QQ,))
        stayed = self._rows("SELECT uid, pid FROM messages WHERE uid=?", ("10002",))
        self.assertEqual([new_pid], [int(row["pid"]) for row in moved])
        self.assertEqual([pid], [int(row["pid"]) for row in stayed])

    def test_unlink_keeps_person_referenced_by_messages(self):
        pid = self._link("aiocqhttp", QQ, 0, "阿墨")
        self._add_message("", "", "user", "没有平台的老消息", pid)
        self.assertTrue(self.persons.unlink_identity(BOT, "aiocqhttp", QQ))
        left = self.db.conn.execute("SELECT pid FROM persons WHERE pid=?", (pid,)).fetchone()
        self.assertIsNotNone(left, "还有消息引用时不能删人物，否则 pid 悬空")

    def test_unlink_unknown_identity_returns_false(self):
        self.assertFalse(self.persons.unlink_identity(BOT, "lark", "nobody"))

    def test_adopt_orphan_is_idempotent(self):
        pid = self._link("aiocqhttp", QQ, 0, "阿墨")
        self._add_memory("999", "孤儿：主人买了大米")
        self.assertEqual(1, self.persons.adopt_orphan_memories())
        row = self._rows("SELECT pid, uid FROM memories")[0]
        self.assertEqual(pid, int(row["pid"]))
        self.assertEqual(QQ, str(row["uid"]))
        self.assertEqual(0, self.persons.adopt_orphan_memories(), "第二次不许重复归位")
        self.assertEqual(1, int(self.db.conn.execute(
            "SELECT COUNT(*) FROM memories").fetchone()[0]))

    def test_adopt_linked_uid_gets_pid_even_with_many_persons(self):
        pid = self._link("aiocqhttp", QQ, 0, "阿墨")
        self._link("lark", "ou_other", 0, "路人")
        self._add_memory(QQ, "uid 已绑定但 pid 还是 0")
        self.assertEqual(1, self.persons.adopt_orphan_memories())
        row = self._rows("SELECT pid, uid FROM memories")[0]
        self.assertEqual(pid, int(row["pid"]))
        self.assertEqual(QQ, str(row["uid"]))

    def test_ghost_merge_skips_person_without_any_activity(self):
        pid = self._link("lark", LARK, 0, "阿墨")
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO profiles(bot_id, uid, name, updated_at) VALUES(?,?,?,?)",
                (BOT, LARK, "阿墨", 1700000000),
            )
            self.db.conn.commit()
        empty = self._link("aiocqhttp", "777", 0, "刚绑的")
        self.assertEqual(0, self.persons.merge_ghost_persons(),
                         "没有任何消息的空人物不能当幽灵并掉")
        self.assertTrue(self.persons.person_detail(BOT, empty))
        self._add_message("aiocqhttp", "777", "summary", "[摘要] 在吗", empty)
        self.assertEqual(1, self.persons.merge_ghost_persons())
        self.assertEqual({}, self.persons.person_detail(BOT, empty))
        row = self._rows("SELECT pid FROM messages WHERE role='summary'")
        self.assertEqual([pid], [int(item["pid"]) for item in row])

    def test_resolve_person_invalidates_canonical_cache(self):
        pid = self._link("aiocqhttp", QQ, 0, "阿墨")
        self.assertEqual(QQ, self.persons.canonical_uid(BOT, "aiocqhttp", QQ))
        self.assertEqual(QQ, self.persons.canonical_uid_for_uid(BOT, QQ))
        merged = self.persons.resolve_person(BOT, "lark", LARK, "阿墨")
        self.assertEqual(pid, merged)
        self.assertEqual(LARK, self.persons.canonical_uid(BOT, "aiocqhttp", QQ),
                         "新绑定后缓存必须失效")
        self.assertEqual(LARK, self.persons.canonical_uid_for_uid(BOT, QQ))

    def test_repair_persons_restores_and_is_idempotent(self):
        with self.db.lock:
            self.db.conn.execute(
                "INSERT INTO person_links(bot_id, platform, uid, pid, name, created_at) "
                "VALUES(?,?,?,?,?,?)", (BOT, "lark", "ou_ghost", 77, "她", 1700000000))
            self.db.conn.commit()
        self.assertEqual(1, self.persons.repair_persons())
        self.assertIsNotNone(self.db.conn.execute(
            "SELECT pid FROM persons WHERE pid=77").fetchone())
        self.assertEqual(0, self.persons.repair_persons())
        self.assertEqual([], self.persons.person_invariants(BOT))

    def test_person_model_pure_rules(self):
        links = [{"platform": "aiocqhttp", "uid": "a"},
                 {"platform": "lark", "uid": "b"}]
        self.assertEqual("b", person_model.canonical_link(links))
        self.assertEqual("", person_model.canonical_link([], ""))
        rows = [{"pid": 5, "name": "小明", "platform": "aiocqhttp"}]
        self.assertEqual(5, person_model.auto_merge_target(rows, "小明", "lark"))
        self.assertEqual(0, person_model.auto_merge_target(rows, "小明", "aiocqhttp"))
        rows.append({"pid": 6, "name": "小明", "platform": "qq"})
        self.assertEqual(0, person_model.auto_merge_target(rows, "小明", "lark"))
        issues = person_model.invariants(
            [{"pid": 1, "bot_id": BOT}],
            [{"bot_id": BOT, "platform": "lark", "uid": "u", "pid": 2}])
        self.assertTrue(any("dangling pid" in item for item in issues))
        self.assertFalse(person_model.ghost_is_mergeable(0, 0, 0, 0))
        self.assertTrue(person_model.ghost_is_mergeable(0, 0, 0, 3))
        self.assertEqual("platform", person_model.unlink_message_scope(1))
        self.assertEqual("uid", person_model.unlink_message_scope(2))


class MemoryWriteTests(_StoreCase):
    def test_prepare_write_normalizes_every_field(self):
        prepared = memory_schema.prepare_write(
            content="  他  喜欢 猫 ",
            kind="  ",
            importance=1.7,
            scope="group",
            group_id=" g1 ",
            subject="BOT",
            idem_key="  k  ",
            tags="  猫   宠物 ",
            now=1700000000,
        )
        self.assertTrue(prepared["ok"])
        self.assertEqual("fact", prepared["kind"])
        self.assertEqual("他 喜欢 猫", prepared["text"])
        self.assertEqual(1.0, prepared["importance"])
        self.assertEqual("group", prepared["scope"])
        self.assertEqual(" g1 ", prepared["group_id"])
        self.assertEqual("bot", prepared["subject"])
        self.assertEqual("k", prepared["idem_key"])
        self.assertEqual("猫 宠物", prepared["tags"])
        self.assertTrue(prepared["static"])
        self.assertFalse(memory_schema.prepare_write(content="   ")["ok"])

    def test_prepare_write_importance_and_scope_defaults(self):
        prepared = memory_schema.prepare_write(
            content="一句话", importance="坏", scope="private", group_id="g")
        self.assertEqual(0.5, prepared["importance"])
        self.assertEqual("private", prepared["scope"])
        self.assertEqual("", prepared["group_id"])
        self.assertEqual(120, len(memory_schema.normalize_idem_key("x" * 500)))

    def test_nonstatic_idem_keeps_latest_content(self):
        first = self.memory.remember(BOT, "u1", "事件第一版", kind="event", idem_key="e1")
        second = self.memory.remember(BOT, "u1", "事件第二版", kind="event", idem_key="e1")
        self.assertTrue(first["ok"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["memory_id"], second["memory_id"])
        row = self._rows("SELECT content FROM memories")[0]
        self.assertEqual("事件第二版", str(row["content"]), "同一 idem 不许丢掉最新正文")

    def test_nonstatic_idem_keep_existing_content(self):
        first = self.memory.remember(BOT, "u1", "事件第一版", kind="event", idem_key="e2")
        self.memory.remember(BOT, "u1", "事件第二版", kind="event", idem_key="e2",
                             keep_existing_content=True)
        row = self._rows("SELECT id, content FROM memories")[0]
        self.assertEqual("事件第一版", str(row["content"]))
        self.assertEqual(first["memory_id"], int(row["id"]))

    def test_static_fact_duplicate_merges_without_new_row(self):
        self.memory.remember(BOT, "u1", "我喜欢吃螺蛳粉", kind="preference")
        second = self.memory.remember(BOT, "u1", "我喜欢吃螺蛳粉加辣", kind="preference")
        self.assertTrue(second["duplicate"])
        rows = self._rows("SELECT content FROM memories")
        self.assertEqual(1, len(rows), "静态事实重复写不许新增行")
        self.assertIn("加辣", str(rows[0]["content"]))

    def test_idem_isolated_by_scope(self):
        self.memory.remember(BOT, "u1", "同一句话", kind="event",
                             scope="private", idem_key="same")
        self.memory.remember(BOT, "u1", "同一句话", kind="event",
                             scope="group", group_id="g1", idem_key="same")
        count = int(self.db.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
        self.assertEqual(2, count, "群聊/私聊是两条记忆，去重键必须带 scope")

    def test_tags_survive_retouch_without_tags(self):
        self.memory.remember(BOT, "u1", "我喜欢猫", kind="preference", tags=" 猫  宠物 ")
        row = self._rows("SELECT tags FROM memories")[0]
        self.assertEqual("猫 宠物", str(row["tags"]))
        self.memory.remember(BOT, "u1", "我喜欢猫", kind="preference")
        row = self._rows("SELECT tags FROM memories")[0]
        self.assertEqual("猫 宠物", str(row["tags"]), "空 tags 不许清掉旧标签")

    def test_auto_idem_only_for_static_facts(self):
        self.memory.remember(BOT, "u1", "我喜欢猫", kind="preference")
        rows = self._rows("SELECT idem_key FROM memories WHERE kind='preference'")
        self.assertTrue(str(rows[0]["idem_key"]).startswith("fact:"))
        self.memory.remember(BOT, "u1", "今天下雨了", kind="event")
        rows = self._rows("SELECT idem_key FROM memories WHERE kind='event'")
        self.assertEqual("", str(rows[0]["idem_key"]))

    def test_remember_uses_single_validation_entry(self):
        calls = []
        original = memory_schema.prepare_write

        def spy(**kwargs):
            calls.append(kwargs)
            return original(**kwargs)

        memory_schema.prepare_write = spy
        try:
            result = self.memory.remember(BOT, "u1", "唯一入口", kind="event")
        finally:
            memory_schema.prepare_write = original
        self.assertTrue(result["ok"])
        self.assertEqual(1, len(calls), "remember 必须只走 memory_schema.prepare_write")
        self.assertFalse(memory_schema.is_static_kind("event"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
