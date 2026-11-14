# -*- coding: utf-8 -*-
"""回归：保留策略真的清日志、备份真的按份数裁剪、整理预演真的显示会合并几条。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.backup_engine import BackupEngine  # noqa: E402
from services.maintenance_engine import MaintenanceEngine  # noqa: E402
from store import DataStore  # noqa: E402

BOT = "b"
UID = "302578963"


class FakeLogs:
    def __init__(self):
        self.calls = []

    def purge_logs(self, bot_id, days):
        self.calls.append((bot_id, days))
        return 2


class FakeMessages:
    def compress_messages(self, bot_id, after, days):
        return {"compressed": 1, "after": after, "days": days}


class MaintenanceWiringTests(unittest.TestCase):
    def test_retention_calls_purge_logs(self):
        logs = FakeLogs()
        plugin = SimpleNamespace(logs=logs, messages=FakeMessages(),
                                 db=object(), settings=SimpleNamespace())
        engine = MaintenanceEngine(plugin)
        result = engine.retention(BOT, 14, 3, 7)
        self.assertEqual(result["logs_purged"], 2)
        self.assertEqual(logs.calls, [(BOT, 7)])
        self.assertNotIn("log_error", result)


class PruneBackupsTests(unittest.TestCase):
    def test_keeps_newest_and_removes_rest(self):
        tmp = tempfile.mkdtemp(dir=os.environ.get("AMO_DATA_TEST_TMP") or None)
        for index in range(3):
            path = os.path.join(tmp, "amo_data-2026-11-0%d.db" % (index + 1))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x")
            os.utime(path, (1000 + index, 1000 + index))
        engine = object.__new__(BackupEngine)
        self.assertEqual(engine.prune_backups(tmp, 1), 2)
        left = [n for n in os.listdir(tmp) if n.endswith(".db")]
        self.assertEqual(left, ["amo_data-2026-11-03.db"])


class MergePreviewTests(unittest.TestCase):
    def test_preview_reports_merged_summaries(self):
        tmp = tempfile.mkdtemp(dir=os.environ.get("AMO_DATA_TEST_TMP") or None)
        store = DataStore(os.path.join(tmp, "p.db"))
        try:
            day = "2026-11-01"
            store.remember(BOT, UID, "%s 今天一起去了公园散步" % day, kind="conversation_summary")
            store.remember(BOT, UID, "%s 晚上在家吃了火锅" % day, kind="conversation_summary")
            preview = store.compress_memories(BOT, apply=False)
            self.assertFalse(preview["applied"])
            self.assertGreaterEqual(preview["merged_summaries"], 1)
            # 预演不能改数据：两条都还是 active
            self.assertEqual(len(store.list_memories(BOT, UID, 10)), 2)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
