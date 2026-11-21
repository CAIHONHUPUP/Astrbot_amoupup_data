# -*- coding: utf-8 -*-
"""群 umo 迁移：合并 + 备份 + 报告。"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.message_engine import MessageEngine  # noqa: E402


class _DB:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY, bot_id TEXT, umo TEXT, uid TEXT, "
            "name TEXT, role TEXT, content TEXT, created_at INTEGER, platform TEXT, pid INTEGER, "
            "message_id TEXT, group_name TEXT, compressed INTEGER DEFAULT 0)")
        self.conn.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT)")
        self.conn.commit()


class GroupMigrationTests(unittest.TestCase):
    def test_merges_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.db")
            db = _DB(path)
            for umo in ("aiocqhttp:GroupMessage:123:456", "aiocqhttp:GroupMessage:123:789"):
                db.conn.execute(
                    "INSERT INTO messages(bot_id,umo,uid,name,role,content,created_at,platform) "
                    "VALUES('b',?,'u','n','user','hi',1,'aiocqhttp')", (umo,))
            db.conn.commit()
            engine = MessageEngine(db)
            report = engine._migrate_group_umos()
            self.assertEqual(2, report["merged"])
            rows = db.conn.execute("SELECT DISTINCT umo FROM messages").fetchall()
            self.assertEqual(["aiocqhttp:GroupMessage:123"], [r["umo"] for r in rows])
            self.assertTrue(any(name.startswith("x.db.bak-group-") for name in os.listdir(tmp)))
            meta = db.conn.execute(
                "SELECT value FROM store_meta WHERE key='group_umo_migration'").fetchone()
            self.assertIn("merged", meta["value"])
            db.conn.close()

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _DB(os.path.join(tmp, "x.db"))
            db.conn.execute(
                "INSERT INTO messages(bot_id,umo,uid,name,role,content,created_at,platform) "
                "VALUES('b','aiocqhttp:GroupMessage:123:456','u','n','user','hi',1,'aiocqhttp')")
            db.conn.commit()
            report = MessageEngine(db)._migrate_group_umos(dry_run=True)
            self.assertEqual(1, report["merged"])
            row = db.conn.execute("SELECT umo FROM messages").fetchone()
            self.assertEqual("aiocqhttp:GroupMessage:123:456", row["umo"])
            db.conn.close()


if __name__ == "__main__":
    unittest.main()
