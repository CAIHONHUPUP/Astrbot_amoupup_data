# -*- coding: utf-8 -*-
"""群会话 ID 唯一归一化（task-27，P1-1）。

审计：代码里同一群存在 3 套归一化口径（入库 normalize_group_umo / 群聊页
groupKey / 聊天页 sessionLabel），改名用的 key 与查名用的 key 不是同一个；
一旦有群消息可能出现 2 个群节点 / 改名不生效。

修复后：
- 后端只有 services.message_engine.normalize_group_umo 一套（入库、迁移、分组）；
- 面板 JS 只有 normalizeGroupUmo 一套（聊天页分组、群聊页分组、群名查找都走它），
  groupKey 只是旧名字的别名，不再保留第二套正则。
"""
from __future__ import annotations

import io
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.message_engine import normalize_group_umo  # noqa: E402

PANEL = os.path.join(ROOT, "views", "dashboard", "index.html")


class BackendNormalizeTests(unittest.TestCase):
    def test_qq_group_with_sender_tail(self):
        self.assertEqual(normalize_group_umo("aiocqhttp:GroupMessage:123:456"),
                         "aiocqhttp:GroupMessage:123")
        self.assertEqual(normalize_group_umo("aiocqhttp:GroupMessage:123_456"),
                         "aiocqhttp:GroupMessage:123")

    def test_lark_group_id_without_numeric_tail(self):
        # 飞书群 ID 带字母：只丢 sender 尾段，不截断群 ID 本身
        self.assertEqual(normalize_group_umo("lark:GroupMessage:oc_abc:ou_x"),
                         "lark:GroupMessage:oc_abc")

    def test_private_umo_untouched(self):
        for umo in ("lark:FriendMessage:ou_x", "aiocqhttp:FriendMessage:10001", ""):
            self.assertEqual(normalize_group_umo(umo), umo)


class PanelSingleNormalizeTests(unittest.TestCase):
    def setUp(self):
        with io.open(PANEL, encoding="utf-8") as fh:
            self.html = fh.read()

    def test_panel_has_one_normalizer(self):
        self.assertEqual(self.html.count("function normalizeGroupUmo"), 1)

    def test_group_regex_appears_only_in_normalizer(self):
        # 归一化规则只允许出现在 normalizeGroupUmo 里；sessionLabel/groupLabel
        # 只在归一化后的字符串上取尾段展示（正则带 $ 锚点），不得再实现一套截断。
        normalize = re.findall(r":GroupMessage:\(\[\^:\]\+\)/", self.html)
        self.assertEqual(len(normalize), 1,
                         "群 umo 归一化正则出现了 %d 次" % len(normalize))

    def test_session_tree_uses_normalizer(self):
        self.assertIn("const key = normalizeGroupUmo(", self.html)
        self.assertIn('const umo = normalizeGroupUmo(String((item && item.umo) || ""));',
                      self.html)

    def test_group_label_manual_name_uses_normalized_key(self):
        self.assertIn('const manual = String(((window._groupNames || {})[umo]) || "").trim();',
                      self.html)

    def test_old_group_key_is_thin_alias(self):
        # groupKey 只能是别名，不能有第二套实现（截断逻辑只允许在 normalizeGroupUmo 里）
        self.assertIn("function groupKey(umo) {", self.html)
        self.assertEqual(self.html.count("return normalizeGroupUmo(umo);"), 1)
        self.assertEqual(self.html.count("gid.split"), 1)


if __name__ == "__main__":
    unittest.main()
