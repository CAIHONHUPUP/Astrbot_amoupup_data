# -*- coding: utf-8 -*-
"""记忆主键升级到 pid：跨平台一致、人物之间隔离、未绑定有兜底。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from store import DataStore  # noqa: E402

BOT = "bot1"
QQ = "302578963"
LARK = "ou_cec66ea605ad17f62632fb044bc5f6ac"


class PersonIdMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-pid-")
        self.path = os.path.join(self.tmp, "amo.db")
        self.store = DataStore(self.path)

    def tearDown(self):
        self.store.close()

    def _link_both(self):
        pid = self.store.link_identity(BOT, "aiocqhttp", QQ, 0, "family")
        self.store.link_identity(BOT, "lark", LARK, pid, "family")
        return pid

    # ① 同一个人两个平台 → 同一个 pid、同一个记忆桶
    def test_both_platforms_share_pid_and_memory(self):
        pid = self._link_both()
        self.store.remember(BOT, QQ, "他喜欢猫", platform="aiocqhttp")
        self.store.remember(BOT, LARK, "他明天去海边", platform="lark")
        rows = self.store.conn.execute(
            "SELECT pid, uid, platform FROM memories WHERE bot_id=?", (BOT,)
        ).fetchall()
        self.assertEqual({int(r["pid"]) for r in rows}, {int(pid)}, "两条记忆要挂在同一个 pid 上")
        self.assertEqual({str(r["uid"]) for r in rows}, {LARK}, "uid 归一成主 uid")
        hits = self.store.recall(BOT, QQ, "海边", platform="aiocqhttp")
        self.assertTrue(hits, "QQ 这头要能想起飞书说过的话")
        self.assertTrue(any("海边" in str(h["content"]) for h in hits))

    # ② 不同的人之间不能串
    def test_different_people_isolated(self):
        pid_a = self._link_both()
        pid_b = self.store.link_identity(BOT, "aiocqhttp", "10001", 0, "路人")
        self.assertNotEqual(pid_a, pid_b)
        self.store.remember(BOT, "10001", "他喜欢狗", platform="aiocqhttp")
        self.assertFalse(self.store.recall(BOT, QQ, "喜欢狗", platform="aiocqhttp"),
                         "B 的记忆不能出现在 A 的召回里")
        rows = self.store.conn.execute(
            "SELECT pid FROM memories WHERE bot_id=? AND content LIKE '%狗%'", (BOT,)
        ).fetchall()
        self.assertEqual([int(r["pid"]) for r in rows], [pid_b])

    # ③ 未绑定：不报错，自动建一个人物，按 pid 存；别的平台查不到
    def test_unbound_creates_person_and_stays_private(self):
        result = self.store.remember(BOT, LARK, "只有飞书说过", platform="lark")
        self.assertTrue(result.get("ok"))
        row = self.store.conn.execute(
            "SELECT pid, platform, uid FROM memories WHERE bot_id=?", (BOT,)
        ).fetchone()
        self.assertGreater(int(row["pid"]), 0, "写前 resolve：未绑定也会建人物")
        self.assertEqual(str(row["platform"]), "lark")
        self.assertEqual(str(row["uid"]), LARK, "没绑定时 uid 保持平台 id")
        self.assertFalse(self.store.recall(BOT, QQ, "只有飞书说过", platform="aiocqhttp"))

    # ④ 记忆行记录来源平台
    def test_memory_remembers_source_platform(self):
        self._link_both()
        self.store.remember(BOT, QQ, "他喜欢猫", platform="aiocqhttp")
        self.store.remember(BOT, LARK, "他喜欢猫", platform="lark")  # 同一句会被去重合并
        rows = self.store.list_memories(BOT, uid=LARK)
        self.assertTrue(rows)
        self.assertEqual({str(r["platform"]) for r in rows}, {"aiocqhttp"})

    # ⑤ 按 pid 列记忆：一次看全两个平台
    def test_list_memories_by_pid(self):
        pid = self._link_both()
        self.store.remember(BOT, QQ, "他喜欢猫", platform="aiocqhttp")
        self.store.remember(BOT, LARK, "他明天去海边", platform="lark")
        rows = self.store.list_memories(BOT, limit=50, pid=pid)
        self.assertEqual(len(rows), 2)
        self.assertEqual({str(r["platform"]) for r in rows}, {"aiocqhttp", "lark"})

    # ⑥ 老数据（pid=0）重开库后自动回填 pid，仍然查得到
    def test_backfill_pid_for_legacy_rows(self):
        pid = self._link_both()
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO memories(bot_id, uid, content, created_at, last_hit_at, importance) "
                "VALUES(?,?,?,?,?,?)",
                (BOT, LARK, "老记忆：他养了猫", 1_700_000_000, 1_700_000_000, 0.6),
            )
            self.store.conn.commit()
        self.store.close()
        self.store = DataStore(self.path)
        row = self.store.conn.execute(
            "SELECT pid FROM memories WHERE bot_id=? AND content LIKE '%老记忆%'", (BOT,)
        ).fetchone()
        self.assertEqual(int(row["pid"]), int(pid), "启动迁移要把 pid 补上")
        self.assertTrue(self.store.recall(BOT, QQ, "老记忆", platform="aiocqhttp"))

    # ⑦ 她的发言行（uid='bot'）也要按 umo 尾部 uid 回填 pid
    def test_backfill_pid_for_bot_rows(self):
        pid = self._link_both()
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, platform, pid) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (BOT, "bot1:FriendMessage:" + QQ, "bot", "她", "bot", "她说过的话",
                 1_700_000_100, "aiocqhttp", 0),
            )
            self.store.conn.commit()
        self.store.close()
        self.store = DataStore(self.path)
        row = self.store.conn.execute(
            "SELECT pid FROM messages WHERE bot_id=? AND role='bot'", (BOT,)
        ).fetchone()
        self.assertEqual(int(row["pid"]), int(pid), "她的发言行也要挂到这个人 pid 上")

    # ⑧ 人物表自愈：身份指向不存在的人物时，启动补回人物
    def test_repair_persons_restores_missing_person(self):
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO person_links(bot_id, platform, uid, pid, name, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (BOT, "lark", "ou_ghost", 77, "她", 1_700_000_000),
            )
            self.store.conn.commit()
        self.assertIsNone(
            self.store.conn.execute("SELECT pid FROM persons WHERE pid=77").fetchone()
        )
        self.store.close()
        self.store = DataStore(self.path)
        row = self.store.conn.execute("SELECT pid, name FROM persons WHERE pid=77").fetchone()
        self.assertIsNotNone(row, "丢失的人物要补回来")
        self.assertEqual(str(row["name"]), "她")

    # ⑨ 孤儿记忆：库里只有一个人时，归给他
    def test_orphan_memories_adopted_by_single_person(self):
        pid = self._link_both()
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO memories(bot_id, uid, content, created_at, last_hit_at, importance) "
                "VALUES(?,?,?,?,?,?)",
                (BOT, "470488586", "孤儿：主人那天吃了螺蛳粉", 1_700_000_000, 1_700_000_000, 0.5),
            )
            self.store.conn.commit()
        self.store.close()
        self.store = DataStore(self.path)
        row = self.store.conn.execute(
            "SELECT pid, uid FROM memories WHERE content LIKE '%孤儿%'"
        ).fetchone()
        self.assertEqual(int(row["pid"]), int(pid))
        self.assertEqual(str(row["uid"]), LARK, "uid 要归一成主 uid")
        self.assertTrue(self.store.recall(BOT, QQ, "螺蛳粉", platform="aiocqhttp"))

    # ⑩ 多个人物时不猜：孤儿保持 pid=0，等人来绑
    def test_orphan_memories_not_guessed_when_many_persons(self):
        self._link_both()
        self.store.link_identity(BOT, "aiocqhttp", "10001", 0, "路人")
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO memories(bot_id, uid, content, created_at, last_hit_at, importance) "
                "VALUES(?,?,?,?,?,?)",
                (BOT, "999", "孤儿：不确定是谁", 1_700_000_000, 1_700_000_000, 0.5),
            )
            self.store.conn.commit()
        self.store.close()
        self.store = DataStore(self.path)
        row = self.store.conn.execute(
            "SELECT pid FROM memories WHERE content LIKE '%不确定%'"
        ).fetchone()
        self.assertEqual(int(row["pid"]), 0, "多个人时不猜，避免串味")

    # ⑪ 机器人自己 uid 下的记忆（alias self）归给消息最多的人
    def test_self_uid_memories_go_to_owner(self):
        pid = self._link_both()
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO identity_alias(bot_id, platform, uid, alias_platform, alias_uid, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (BOT, "aiocqhttp", "self", "aiocqhttp", "470488586", 1_700_000_000),
            )
            self.store.conn.execute(
                "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, platform, pid) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (BOT, "bot1:FriendMessage:" + QQ, QQ, "family", "user", "在吗", 1_700_000_000, "aiocqhttp", pid),
            )
            self.store.conn.execute(
                "INSERT INTO memories(bot_id, uid, content, created_at, last_hit_at, importance) "
                "VALUES(?,?,?,?,?,?)",
                (BOT, "470488586", "孤儿：主人买了大米", 1_700_000_000, 1_700_000_000, 0.5),
            )
            self.store.conn.commit()
        self.store.close()
        self.store = DataStore(self.path)
        row = self.store.conn.execute(
            "SELECT pid, uid FROM memories WHERE content LIKE '%大米%'"
        ).fetchone()
        self.assertEqual(int(row["pid"]), int(pid), "self 记账的记忆要归给主人")
        self.assertEqual(str(row["uid"]), LARK)

    # ⑫ 幽灵人物（只有摘要、无档案无记忆）并入唯一有档案的人
    def test_ghost_person_merged_into_profiled_one(self):
        pid = self._link_both()
        self.store.save_profile(BOT, QQ, name="family", relation="相恋")
        ghost = self.store.link_identity(BOT, "lark", "ou_ghost", 0, "她")
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, platform, pid) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (BOT, "lark:FriendMessage:ou_ghost", "ou_ghost", "她", "summary",
                 "[2026-11-07 共 3 条] 她：在吗", 1_700_000_000, "lark", ghost),
            )
            self.store.conn.commit()
        self.store.close()
        self.store = DataStore(self.path)
        left = self.store.conn.execute("SELECT pid FROM persons WHERE pid=?", (ghost,)).fetchone()
        self.assertIsNone(left, "幽灵人物应该被并掉")
        row = self.store.conn.execute(
            "SELECT pid FROM messages WHERE content LIKE '%在吗%'"
        ).fetchone()
        self.assertEqual(int(row["pid"]), int(pid), "摘要行要并到主人名下")

    # ⑬ 来源回填：同一句话出现在聊天记录里 → 用那条消息的平台
    def test_backfill_platform_by_same_content(self):
        pid = self._link_both()
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, platform, pid) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (BOT, "bot1:FriendMessage:" + QQ, QQ, "family", "user", "他今天吃了火锅",
                 1_700_000_000, "aiocqhttp", pid),
            )
            self.store.conn.execute(
                "INSERT INTO memories(bot_id, uid, pid, content, created_at, last_hit_at, importance) "
                "VALUES(?,?,?,?,?,?,?)",
                (BOT, LARK, pid, "他今天吃了火锅", 1_700_000_000, 1_700_000_000, 0.5),
            )
            self.store.conn.commit()
        res = self.store.backfill_memory_platform()
        self.assertEqual(res["filled"], 1)
        row = self.store.conn.execute(
            "SELECT platform FROM memories WHERE content='他今天吃了火锅'").fetchone()
        self.assertEqual(str(row["platform"]), "aiocqhttp")

    # ⑭ 来源回填：前后 10 分钟只有一个平台在说话 → 用它
    def test_backfill_platform_by_time_window(self):
        pid = self._link_both()
        with self.store.lock:
            self.store.conn.execute(
                "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, platform, pid) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (BOT, "lark:FriendMessage:" + LARK, LARK, "family", "user", "随便聊聊天气",
                 1_700_000_300, "lark", pid),
            )
            self.store.conn.execute(
                "INSERT INTO memories(bot_id, uid, pid, content, created_at, last_hit_at, importance) "
                "VALUES(?,?,?,?,?,?,?)",
                (BOT, LARK, pid, "他问了今天天气", 1_700_000_200, 1_700_000_200, 0.5),
            )
            self.store.conn.commit()
        res = self.store.backfill_memory_platform()
        self.assertEqual(res["filled"], 1)
        row = self.store.conn.execute(
            "SELECT platform FROM memories WHERE content='他问了今天天气'").fetchone()
        self.assertEqual(str(row["platform"]), "lark")

    # ⑮ 两个平台都在说话 → 不猜，保持未知
    def test_backfill_platform_ambiguous_left_empty(self):
        pid = self._link_both()
        with self.store.lock:
            for ts, plat in ((1_700_000_100, "aiocqhttp"), (1_700_000_200, "lark")):
                self.store.conn.execute(
                    "INSERT INTO messages(bot_id, umo, uid, name, role, content, created_at, platform, pid) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (BOT, plat + ":FriendMessage:x", QQ, "family", "user", "混着说" + plat, ts, plat, pid),
                )
            self.store.conn.execute(
                "INSERT INTO memories(bot_id, uid, pid, content, created_at, last_hit_at, importance) "
                "VALUES(?,?,?,?,?,?,?)",
                (BOT, LARK, pid, "说不清哪边说的", 1_700_000_150, 1_700_000_150, 0.5),
            )
            self.store.conn.commit()
        res = self.store.backfill_memory_platform()
        self.assertEqual(res["filled"], 0)
        self.assertEqual(res["ambiguous"], 1)
        row = self.store.conn.execute(
            "SELECT platform FROM memories WHERE content='说不清哪边说的'").fetchone()
        self.assertEqual(str(row["platform"] or ""), "")

    # ⑯ 回填幂等：第二次不会再补
    def test_backfill_platform_idempotent(self):
        self.test_backfill_platform_by_same_content()
        again = self.store.backfill_memory_platform()
        self.assertEqual(again["filled"], 0)


if __name__ == "__main__":
    unittest.main()
