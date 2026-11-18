# -*- coding: utf-8 -*-
"""类型化 TTL / 半衰期 / 流水账提前归档的单测。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import memory_decay  # noqa: E402
from support.store_fixture import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class HalfLifeTests(unittest.TestCase):
    def test_noise_lives_short(self):
        self.assertEqual(memory_decay.half_life_for("activity", 45.0), 7.0)
        self.assertEqual(memory_decay.half_life_for("finance", 45.0), 7.0)

    def test_facts_live_long(self):
        self.assertEqual(memory_decay.half_life_for("fact", 45.0), 3650.0)
        self.assertEqual(memory_decay.half_life_for("preference", 45.0), 3650.0)

    def test_unknown_kind_uses_default(self):
        self.assertEqual(memory_decay.half_life_for("whatever", 30.0), 30.0)
        self.assertEqual(memory_decay.half_life_for("", 0), 45.0)

    def test_noise_kind_list_is_sorted_and_extendable(self):
        kinds = memory_decay.noise_kind_list(["extra_kind"])
        self.assertEqual(kinds, sorted(kinds))
        self.assertIn("extra_kind", kinds)
        self.assertIn("activity", kinds)


class NoiseArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"), settings={
            "embedding_enabled": False, "auto_dedupe": False,
        })
        self.noise_id = self.store.remember(
            BOT, UID, "2026-10-01 流水账：下午三点喝了杯咖啡", kind="activity",
            importance=0.3)["memory_id"]
        self.fact_id = self.store.remember(
            BOT, UID, "主人对芒果过敏", kind="fact", importance=0.9)["memory_id"]
        self.same_age_fact = self.store.remember(
            BOT, UID, "主人喜欢喝冰美式", kind="preference", importance=0.5)["memory_id"]
        stale = int(time.time()) - 40 * 86400
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE memories SET created_at=?, last_hit_at=? WHERE id IN (?,?,?)",
                (stale, stale, self.noise_id, self.fact_id, self.same_age_fact),
            )
            self.store.conn.commit()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _status(self, memory_id):
        row = self.store.conn.execute(
            "SELECT status FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        return str(row["status"])

    def test_noise_archived_before_90_days(self):
        out = self.store.compression.archive_stale(BOT, 90, 0.3, apply=True)
        self.assertEqual(self._status(self.noise_id), "archived")
        self.assertGreaterEqual(out["noise"], 1)
        self.assertEqual(self._status(self.fact_id), "active", "高重要度事实不许提前归档")

    def test_important_activity_survives(self):
        with self.store.lock:
            self.store.conn.execute(
                "UPDATE memories SET importance=0.9 WHERE id=?", (self.noise_id,)
            )
            self.store.conn.commit()
        self.store.compression.archive_stale(BOT, 90, 0.3, apply=True)
        self.assertEqual(self._status(self.noise_id), "active",
                         "重要的活动记录不能按流水账归档")

    def test_preference_uses_long_ttl(self):
        self.store.compression.archive_stale(BOT, 90, 0.3, apply=True)
        self.assertEqual(self._status(self.same_age_fact), "active")


if __name__ == "__main__":
    unittest.main()
