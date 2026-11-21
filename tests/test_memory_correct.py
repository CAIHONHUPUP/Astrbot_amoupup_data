# -*- coding: utf-8 -*-
"""纠错闭环：旧记忆 superseded + 新说法可召回 + 写失败不动旧数据。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from support.store_fixture import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"


def _tmpdir() -> str:
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base)


class CorrectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _tmpdir()
        self.store = DataStore(os.path.join(self.tmp, "amo_data.db"), settings={
            "embedding_enabled": False, "auto_dedupe": False,
        })
        self.old_id = self.store.remember(
            BOT, UID, "主人喜欢吃螺蛳粉", kind="preference", importance=0.6)["memory_id"]


    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _status(self, memory_id):
        row = self.store.conn.execute(
            "SELECT status, superseded_by FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        return str(row["status"]), int(row["superseded_by"] or 0)

    def test_supersede_and_write(self):
        result = self.store.memory.correct(
            BOT, UID, "螺蛳粉", "主人不吃螺蛳粉，闻到味就躲")
        self.assertTrue(result["ok"])
        # 写路径自己也会按负状态覆盖同槽位事实，所以这里只要求「至少旧的那条失效」
        self.assertGreaterEqual(result["superseded"], 1)
        status, by = self._status(self.old_id)
        self.assertEqual(status, "superseded")
        self.assertEqual(by, int(result["memory_id"]))
        rows = self.store.recall(BOT, UID, "螺蛳粉")
        self.assertTrue(rows)
        self.assertIn("闻到味就躲", rows[0]["content"], "召回应该只看到纠正后的说法")

    def test_short_input_rejected(self):
        result = self.store.memory.correct(BOT, UID, "螺", "不吃")
        self.assertFalse(result["ok"])
        self.assertEqual(self._status(self.old_id)[0], "active")

    def test_superseded_rows_are_not_recalled(self):
        # 反向验证：状态过滤真的生效，旧说法不能再被召回
        self.store.memory.correct(BOT, UID, "螺蛳粉", "主人不吃螺蛳粉，闻到味就躲")
        rows = self.store.recall(BOT, UID, "喜欢吃螺蛳粉")
        self.assertTrue(all("喜欢吃螺蛳粉" not in row["content"] for row in rows),
                        "superseded 的记忆不许再进召回")


if __name__ == "__main__":
    unittest.main()
