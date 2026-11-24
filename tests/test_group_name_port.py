# -*- coding: utf-8 -*-
"""群名 / 群会话单测（从 repo 的 test_group_name.py 迁到新布局）。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _harness import Stack  # noqa: E402

BOT = "bot1"
GROUP_UMO = "aiocqhttp:GroupMessage:1055679275"
PRIVATE_UMO = "大肥鱼:FriendMessage:302578963"


class GroupNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="amo-group-")
        self.stack = Stack(self.tmp, "amo.db")

    def tearDown(self):
        self.stack.close()

    def _add(self, umo, uid, name, role, text, **kwargs):
        self.stack.messages.add_message(BOT, umo, uid, name, role, text, **kwargs)

    def test_group_name_roundtrip(self):
        self._add(GROUP_UMO, "10001", "小明", "user", "大家好",
                  platform="aiocqhttp", group_name="测试群")
        rows = self.stack.messages.list_messages_grouped(BOT, umo=GROUP_UMO, limit=10)
        self.assertTrue(rows)
        self.assertEqual(str(rows[0]["group_name"]), "测试群")

    def test_private_message_has_empty_group_name(self):
        self._add(PRIVATE_UMO, "302578963", "family", "user", "在吗", platform="aiocqhttp")
        rows = self.stack.messages.list_messages_grouped(BOT, umo=PRIVATE_UMO, limit=10)
        self.assertEqual(str(rows[0]["group_name"] or ""), "")

    def test_group_name_isolation_between_groups(self):
        self._add(GROUP_UMO, "10001", "小明", "user", "A群消息",
                  platform="aiocqhttp", group_name="A群")
        self._add("aiocqhttp:GroupMessage:999", "10001", "小明", "user", "B群消息",
                  platform="aiocqhttp", group_name="B群")
        a_rows = self.stack.messages.list_messages_grouped(BOT, umo=GROUP_UMO, limit=10)
        b_rows = self.stack.messages.list_messages_grouped(
            BOT, umo="aiocqhttp:GroupMessage:999", limit=10)
        self.assertEqual(str(a_rows[0]["group_name"]), "A群")
        self.assertEqual(str(b_rows[0]["group_name"]), "B群")
        self.assertNotIn("B群消息", [str(r["content"]) for r in a_rows])

    def test_kind_filter_group_only(self):
        self._add(GROUP_UMO, "10001", "小明", "user", "群里的",
                  platform="aiocqhttp", group_name="测试群")
        self._add(PRIVATE_UMO, "302578963", "family", "user", "私聊的", platform="aiocqhttp")
        rows = self.stack.messages.list_messages_grouped(BOT, limit=50, kind="group")
        self.assertEqual([str(r["content"]) for r in rows], ["群里的"])

    def test_kind_filter_private_only(self):
        self._add(GROUP_UMO, "10001", "小明", "user", "群里的",
                  platform="aiocqhttp", group_name="测试群")
        self._add(PRIVATE_UMO, "302578963", "family", "user", "私聊的", platform="aiocqhttp")
        rows = self.stack.messages.list_messages_grouped(BOT, limit=50, kind="private")
        self.assertEqual([str(r["content"]) for r in rows], ["私聊的"])

    def test_replay_backfills_group_name(self):
        self._add(GROUP_UMO, "10001", "小明", "user", "重放的消息",
                  platform="aiocqhttp", message_id="m-replay")
        self._add(GROUP_UMO, "10001", "小明", "user", "重放的消息",
                  platform="aiocqhttp", message_id="m-replay", group_name="补上的群名")
        rows = self.stack.messages.list_messages_grouped(BOT, umo=GROUP_UMO, limit=10)
        self.assertEqual(len(rows), 1, "同一条 message_id 只能有一行")
        self.assertEqual(str(rows[0]["group_name"]), "补上的群名")

    def test_messages_since_carries_group_name(self):
        self._add(GROUP_UMO, "10001", "小明", "user", "群里的",
                  platform="aiocqhttp", group_name="测试群")
        rows = self.stack.messages.messages_since(BOT, "", 0, 50, umo=GROUP_UMO)
        self.assertEqual(str(rows[0]["group_name"]), "测试群")

    def test_person_context_recent_has_group_name(self):
        self.stack.persons.link_identity(BOT, "aiocqhttp", "10001", 0, "小明")
        self._add(GROUP_UMO, "10001", "小明", "user", "群里的",
                  platform="aiocqhttp", group_name="测试群")
        ctx = self.stack.persons.person_context(BOT, "aiocqhttp", "10001", 5)
        names = [str(r.get("group_name") or "") for r in (ctx.get("recent") or [])]
        self.assertIn("测试群", names)

    def test_group_sessions_aggregates(self):
        self._add(GROUP_UMO, "10001", "小明", "user", "A1",
                  platform="aiocqhttp", group_name="测试群")
        self._add(GROUP_UMO, "10001", "小明", "user", "A2",
                  platform="aiocqhttp", group_name="测试群")
        self._add("aiocqhttp:GroupMessage:999", "10001", "小明", "user", "B1",
                  platform="aiocqhttp")
        rows = {str(r["umo"]): r for r in self.stack.messages.group_sessions(BOT)}
        self.assertEqual(int(rows[GROUP_UMO]["n"]), 2)
        self.assertEqual(str(rows[GROUP_UMO]["group_name"]), "测试群")
        self.assertEqual(int(rows["aiocqhttp:GroupMessage:999"]["n"]), 1)

    def test_rename_group_roundtrip(self):
        self.assertTrue(self.stack.messages.rename_group(BOT, GROUP_UMO, "我的测试群"))
        self.assertEqual(
            self.stack.messages.group_name_overrides(BOT).get(GROUP_UMO), "我的测试群")

    def test_rename_group_rejects_empty(self):
        m = self.stack.messages
        self.assertFalse(m.rename_group(BOT, GROUP_UMO, "   "))
        self.assertFalse(m.rename_group(BOT, "", "名字"))


if __name__ == "__main__":
    unittest.main()
