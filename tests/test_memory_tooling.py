# -*- coding: utf-8 -*-
"""记忆工具层（core/memory_tooling.py）的单测：身份、限额、证据渲染。"""
from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import memory_tooling  # noqa: E402


class _Event:
    def __init__(self, uid="302578963", group="", platform="aiocqhttp"):
        self._uid, self._group, self._platform = uid, group, platform

    def get_sender_id(self):
        return self._uid

    def get_group_id(self):
        return self._group

    def get_platform_name(self):
        return self._platform


class EventIdentityTests(unittest.TestCase):
    def test_private_chat(self):
        ident = memory_tooling.event_identity(_Event())
        self.assertEqual(ident, {"uid": "302578963", "group_id": "", "scope": "private",
                                 "platform": "aiocqhttp"})

    def test_group_chat_is_scoped(self):
        ident = memory_tooling.event_identity(_Event(group="g42"))
        self.assertEqual(ident["scope"], "group")
        self.assertEqual(ident["group_id"], "g42")

    def test_broken_event_does_not_raise(self):
        class Bad:
            def get_sender_id(self):
                raise RuntimeError("boom")

        ident = memory_tooling.event_identity(Bad())
        self.assertEqual(ident["uid"], "")
        self.assertEqual(ident["scope"], "private")

    def test_platform_falls_back_to_umo(self):
        class NoPlatform:
            unified_msg_origin = "lark:123:456"

            def get_sender_id(self):
                return "u1"

        self.assertEqual(memory_tooling.event_identity(NoPlatform())["platform"], "lark")


class LimitAndFormatTests(unittest.TestCase):
    def test_clamp_limit(self):
        self.assertEqual(memory_tooling.clamp_limit(None), 6)
        self.assertEqual(memory_tooling.clamp_limit("3"), 3)
        self.assertEqual(memory_tooling.clamp_limit(999), 20)
        self.assertEqual(memory_tooling.clamp_limit(0), 6, "0 视为没传")
        self.assertEqual(memory_tooling.clamp_limit("x"), 6)

    def test_empty_recall_says_not_found_is_not_never(self):
        text = memory_tooling.format_recall([], 6)
        self.assertEqual(text, memory_tooling.EMPTY_HINT)
        self.assertIn("不代表这件事没发生过", text)

    def test_format_keeps_id_date_and_content(self):
        items = [{"id": 12, "created_at": int(time.time()), "kind": "fact",
                  "score": 0.83, "content": "主人养了一只叫团子的猫"}]
        text = memory_tooling.format_recall(items, 6)
        self.assertIn("id=12", text)
        self.assertIn("fact", text)
        self.assertIn("团子", text)
        self.assertIn("记录", text)

    def test_noisy_kind_is_marked(self):
        items = [{"id": 3, "created_at": int(time.time()), "kind": "finance",
                  "content": "买了一碗热汤面"}]
        self.assertIn("流水账", memory_tooling.format_recall(items, 6))

    def test_limit_caps_rendered_rows(self):
        items = [{"id": index, "created_at": int(time.time()), "kind": "fact",
                  "content": "第%d条" % index} for index in range(9)]
        text = memory_tooling.format_recall(items, 2)
        self.assertIn("id=0", text)
        self.assertIn("id=1", text)
        self.assertNotIn("id=2", text)


if __name__ == "__main__":
    unittest.main()
