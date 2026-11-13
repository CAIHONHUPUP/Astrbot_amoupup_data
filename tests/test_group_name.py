# -*- coding: utf-8 -*-
"""群聊消息带群名入库，面板按群名分类。"""
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
GROUP_UMO = "aiocqhttp:GroupMessage:1055679275"


class GroupNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-group-")
        self.store = DataStore(os.path.join(self.tmp, "amo.db"))

    def tearDown(self):
        self.store.close()

    def test_group_name_roundtrip(self):
        self.store.add_message(BOT, GROUP_UMO, "10001", "小明", "user", "大家好",
                               platform="aiocqhttp", group_name="测试群")
        rows = self.store.list_messages_grouped(BOT, umo=GROUP_UMO, limit=10)
        self.assertTrue(rows)
        self.assertEqual(str(rows[0]["group_name"]), "测试群")

    def test_private_message_has_empty_group_name(self):
        self.store.add_message(BOT, "大肥鱼:FriendMessage:302578963", "302578963",
                               "family", "user", "在吗", platform="aiocqhttp")
        rows = self.store.list_messages_grouped(BOT, umo="大肥鱼:FriendMessage:302578963", limit=10)
        self.assertEqual(str(rows[0]["group_name"] or ""), "")

    def test_group_name_isolation_between_groups(self):
        self.store.add_message(BOT, GROUP_UMO, "10001", "小明", "user", "A群消息",
                               platform="aiocqhttp", group_name="A群")
        self.store.add_message(BOT, "aiocqhttp:GroupMessage:999", "10001", "小明",
                               "user", "B群消息", platform="aiocqhttp", group_name="B群")
        a_rows = self.store.list_messages_grouped(BOT, umo=GROUP_UMO, limit=10)
        b_rows = self.store.list_messages_grouped(BOT, umo="aiocqhttp:GroupMessage:999", limit=10)
        self.assertEqual(str(a_rows[0]["group_name"]), "A群")
        self.assertEqual(str(b_rows[0]["group_name"]), "B群")
        self.assertNotIn("B群消息", [str(r["content"]) for r in a_rows])

    # P1：群聊 / 私聊在库层面过滤，私聊多也不会把群消息挤出 LIMIT 窗口
    def test_kind_filter_group_only(self):
        self.store.add_message(BOT, GROUP_UMO, "10001", "小明", "user", "群里的",
                               platform="aiocqhttp", group_name="测试群")
        self.store.add_message(BOT, "大肥鱼:FriendMessage:302578963", "302578963",
                               "family", "user", "私聊的", platform="aiocqhttp")
        rows = self.store.list_messages_grouped(BOT, limit=50, kind="group")
        self.assertEqual([str(r["content"]) for r in rows], ["群里的"])

    def test_kind_filter_private_only(self):
        self.store.add_message(BOT, GROUP_UMO, "10001", "小明", "user", "群里的",
                               platform="aiocqhttp", group_name="测试群")
        self.store.add_message(BOT, "大肥鱼:FriendMessage:302578963", "302578963",
                               "family", "user", "私聊的", platform="aiocqhttp")
        rows = self.store.list_messages_grouped(BOT, limit=50, kind="private")
        self.assertEqual([str(r["content"]) for r in rows], ["私聊的"])

    # P1：重放/重连时同一条 message_id 不再插入，但会把当时缺的群名补上
    def test_replay_backfills_group_name(self):
        self.store.add_message(BOT, GROUP_UMO, "10001", "小明", "user", "重放的消息",
                               platform="aiocqhttp", message_id="m-replay")
        self.store.add_message(BOT, GROUP_UMO, "10001", "小明", "user", "重放的消息",
                               platform="aiocqhttp", message_id="m-replay", group_name="补上的群名")
        rows = self.store.list_messages_grouped(BOT, umo=GROUP_UMO, limit=10)
        self.assertEqual(len(rows), 1, "同一条 message_id 只能有一行")
        self.assertEqual(str(rows[0]["group_name"]), "补上的群名")

    # P2-3：跨平台近聊 / 时间回看也带群名
    def test_messages_since_carries_group_name(self):
        self.store.add_message(BOT, GROUP_UMO, "10001", "小明", "user", "群里的",
                               platform="aiocqhttp", group_name="测试群")
        rows = self.store.messages_since(BOT, "", 0, 50, umo=GROUP_UMO)
        self.assertEqual(str(rows[0]["group_name"]), "测试群")

    def test_person_context_recent_has_group_name(self):
        self.store.link_identity(BOT, "aiocqhttp", "10001", 0, "小明")
        self.store.add_message(BOT, GROUP_UMO, "10001", "小明", "user", "群里的",
                               platform="aiocqhttp", group_name="测试群")
        ctx = self.store.person_context(BOT, "aiocqhttp", "10001", 5)
        names = [str(r.get("group_name") or "") for r in (ctx.get("recent") or [])]
        self.assertIn("测试群", names)

    # P2-5：手动给群起名
    def test_group_sessions_aggregates(self):
        self.store.add_message(BOT, GROUP_UMO, "10001", "小明", "user", "A1",
                               platform="aiocqhttp", group_name="测试群")
        self.store.add_message(BOT, GROUP_UMO, "10001", "小明", "user", "A2",
                               platform="aiocqhttp", group_name="测试群")
        self.store.add_message(BOT, "aiocqhttp:GroupMessage:999", "10001", "小明",
                               "user", "B1", platform="aiocqhttp")
        rows = {str(r["umo"]): r for r in self.store.group_sessions(BOT)}
        self.assertEqual(int(rows[GROUP_UMO]["n"]), 2)
        self.assertEqual(str(rows[GROUP_UMO]["group_name"]), "测试群")
        self.assertEqual(int(rows["aiocqhttp:GroupMessage:999"]["n"]), 1)

    def test_rename_group_roundtrip(self):
        self.assertTrue(self.store.rename_group(BOT, GROUP_UMO, "我的测试群"))
        self.assertEqual(self.store.group_name_overrides(BOT).get(GROUP_UMO), "我的测试群")

    def test_rename_group_rejects_empty(self):
        self.assertFalse(self.store.rename_group(BOT, GROUP_UMO, "   "))
        self.assertFalse(self.store.rename_group(BOT, "", "名字"))


if __name__ == "__main__":
    unittest.main()
