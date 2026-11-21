# -*- coding: utf-8 -*-
"""群会话 ID 规范化：一个群不能因为 sender 尾段被拆成多份。"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.message_engine import normalize_group_umo  # noqa: E402


class GroupUmoTests(unittest.TestCase):
    def test_sender_suffix_is_removed(self):
        self.assertEqual("aiocqhttp:GroupMessage:123",
                         normalize_group_umo("aiocqhttp:GroupMessage:123:456"))
        self.assertEqual("aiocqhttp:GroupMessage:123",
                         normalize_group_umo("aiocqhttp:GroupMessage:123:789"))

    def test_underscore_sender_suffix_is_removed(self):
        self.assertEqual("aiocqhttp:GroupMessage:123",
                         normalize_group_umo("aiocqhttp:GroupMessage:123_456"))

    def test_plain_group_kept(self):
        self.assertEqual("aiocqhttp:GroupMessage:123",
                         normalize_group_umo("aiocqhttp:GroupMessage:123"))

    def test_private_kept(self):
        self.assertEqual("lark:FriendMessage:ou_x",
                         normalize_group_umo("lark:FriendMessage:ou_x"))


if __name__ == "__main__":
    unittest.main()
