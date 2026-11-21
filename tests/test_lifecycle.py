# -*- coding: utf-8 -*-
"""生命周期：按 kind 的休眠/过期 + 命中复活。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import lifecycle  # noqa: E402
from support.store_fixture import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


def _row(rid, kind, days, importance=0.3, hits=0):
    now = int(time.time())
    return {"id": rid, "kind": kind, "importance": importance, "hit_count": hits,
            "created_at": now - days * 86400, "last_hit_at": now - days * 86400}


class PlanTests(unittest.TestCase):
    def test_noise_goes_dormant(self):
        dormant, expired = lifecycle.plan([_row(1, "activity", 40)], int(time.time()))
        self.assertEqual(dormant, [1])
        self.assertEqual(expired, [])

    def test_fact_never_dormant(self):
        dormant, expired = lifecycle.plan([_row(1, "fact", 4000)], int(time.time()))
        self.assertEqual((dormant, expired), ([], []))

    def test_old_noise_expires(self):
        dormant, expired = lifecycle.plan([_row(1, "activity", 400)], int(time.time()))
        self.assertEqual(expired, [1])
        self.assertEqual(dormant, [])

    def test_important_or_hit_protected(self):
        rows = [_row(1, "activity", 60, importance=0.9), _row(2, "activity", 60, hits=3)]
        self.assertEqual(lifecycle.plan(rows, int(time.time())), ([], []))

    def test_unknown_kind_uses_year(self):
        dormant, _ = lifecycle.plan([_row(1, "whatever", 200)], int(time.time()))
        self.assertEqual(dormant, [])


class PolicyTests(unittest.TestCase):
    def test_should_run_catch_up(self):
        now = int(time.time())
        self.assertTrue(lifecycle.should_run(0, now))
        self.assertTrue(lifecycle.should_run(now - 21 * 3600, now))
        self.assertFalse(lifecycle.should_run(now - 19 * 3600, now))
        self.assertTrue(lifecycle.should_run("bad", now))


class LifecycleStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"), settings={
            "embedding_enabled": False, "auto_dedupe": False,
            "recall_entity_graph": False, "recall_revive_dormant": True,
        })
        self.old_noise = self.store.remember(
            BOT, UID, "2026-10-01 下午喝了杯咖啡", kind="activity", importance=0.3)["memory_id"]
        self.fact = self.store.remember(
            BOT, UID, "主人对芒果过敏", kind="fact", importance=0.9)["memory_id"]
        old = int(time.time()) - 40 * 86400
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE memories SET created_at=?, last_hit_at=? WHERE id=?",
                (old, old, self.old_noise))
            self.store.conn.commit()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _status(self, memory_id):
        row = self.store.conn.execute(
            "SELECT status FROM memories WHERE id=?", (memory_id,)).fetchone()
        return str(row["status"])

    def test_run_lifecycle_marks_dormant(self):
        out = self.store.memory.run_lifecycle(BOT, apply=True)
        self.assertGreaterEqual(out["dormant"], 1)
        self.assertEqual(self._status(self.old_noise), "dormant")
        self.assertEqual(self._status(self.fact), "active")

    def test_dormant_not_recalled_without_match(self):
        self.store.memory.run_lifecycle(BOT, apply=True)
        self.assertEqual(self.store.recall(BOT, UID, "外面下雨了吗"), [])

    def test_strong_match_revives_dormant(self):
        self.store.memory.run_lifecycle(BOT, apply=True)
        rows = self.store.recall(BOT, UID, "咖啡")
        self.assertTrue(rows)
        self.assertEqual(self._status(self.old_noise), "active", "命中后要复活")

    def test_preview_does_not_change_status(self):
        out = self.store.memory.preview_lifecycle(BOT)
        self.assertEqual(out["dormant"], 1)
        self.assertEqual(out["expired"], 0)
        self.assertEqual(self._status(self.old_noise), "active")
        self.assertTrue(out["sample"], "预览要给出样例")
        self.assertIn("counts", out)

    def test_noise_days_override(self):
        self.store.settings["lifecycle_noise_days"] = 1
        out = self.store.memory.run_lifecycle(BOT, apply=True)
        self.assertGreaterEqual(out["dormant"], 1)
        self.assertEqual(self._status(self.fact), "active")

    def test_expire_and_purge(self):
        old = int(time.time()) - 400 * 86400
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE memories SET created_at=?, last_hit_at=? WHERE id=?",
                (old, old, self.old_noise))
            self.store.conn.commit()
        self.store.settings["lifecycle_expire_days"] = 300
        self.store.memory.run_lifecycle(BOT, apply=True)
        self.assertEqual(self._status(self.old_noise), "expired")
        self.assertIsNotNone(self.store.conn.execute(
            "SELECT content FROM memories WHERE id=?", (self.old_noise,)).fetchone(),
            "默认只标记不删")

    def test_purge_deletes_expired_when_enabled(self):
        old = int(time.time()) - 400 * 86400
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE memories SET created_at=?, last_hit_at=? WHERE id=?",
                (old, old, self.old_noise))
            self.store.conn.commit()
        self.store.settings["lifecycle_expire_days"] = 300
        self.store.settings["lifecycle_purge_expired_days"] = 1
        out = self.store.memory.run_lifecycle(BOT, apply=True)
        self.assertEqual(out["purged"], 1)
        self.assertIsNone(self.store.conn.execute(
            "SELECT id FROM memories WHERE id=?", (self.old_noise,)).fetchone())

    def test_revive_can_be_disabled(self):
        self.store.settings["recall_revive_dormant"] = False
        self.store.memory.run_lifecycle(BOT, apply=True)
        self.assertEqual(self.store.recall(BOT, UID, "咖啡"), [])


if __name__ == "__main__":
    unittest.main()
