# -*- coding: utf-8 -*-
"""命名契约：metadata.name 必须等于插件目录名；源码/测试不得硬编码旧目录名。

旧目录名只允许出现在 legacy 回退白名单里（读取旧数据 / 找旧 sidecar / 迁移），
而且即使在这些文件里也不允许 import 旧目录；其余任何位置出现旧名都算回归。
"""
from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_DIR_NAME = ROOT.name

# 家族改名前的旧目录名（含 photo 旧包名 astrbot_plugin_omnidraw）。
LEGACY_DIR_NAMES = (
    "astrbot_plugin_omnidraw",
    "astrbot_plugin_Astrbot_amoupup_forever",
    "astrbot_plugin_Astrbot_Amo_forever",
    "astrbot_plugin_amo_forever",
    "Astr_Amo_data",
    "astrbot_plugin_amo_data",
    "AstrAmoData",
)

# legacy 回退白名单：这些文件允许出现旧目录名（只用于找旧数据/旧 sidecar）。
# 相对 ROOT 的 POSIX 路径；新增条目请在注释里写清回退原因。
LEGACY_FALLBACK_ALLOWLIST = {
    "tests/test_cross_platform.py",
}

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.MULTILINE)
IMPORT_MODULE_RE = re.compile(r"import_module\(\s*[\"\']([^\"\']+)[\"\']")


def read_metadata_name():
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    raise AssertionError("metadata.yaml 缺少 name 字段")


def iter_py_files():
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


class PluginNamingContractTests(unittest.TestCase):
    def test_metadata_name_matches_plugin_directory(self):
        # 插件目录改名而 metadata.name 没跟着改（或反过来）时立刻变红。
        self.assertEqual(read_metadata_name(), PLUGIN_DIR_NAME)

    def test_allowlist_entries_still_exist(self):
        # 白名单不许留僵尸条目，否则回退代码删了也没人知道。
        for rel in LEGACY_FALLBACK_ALLOWLIST:
            self.assertTrue((ROOT / rel).is_file(), "白名单文件不存在：%s" % rel)

    def test_no_legacy_directory_import(self):
        # 回归点：旧测试曾经 import 旧包名（CI 报 No module named ...）。
        for path in iter_py_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            modules = IMPORT_RE.findall(text) + IMPORT_MODULE_RE.findall(text)
            for module in modules:
                for legacy in LEGACY_DIR_NAMES:
                    self.assertNotIn(
                        legacy, module,
                        "禁止 import 旧目录名 %r：%s" % (legacy, path),
                    )

    def test_no_legacy_dir_outside_fallback_allowlist(self):
        for path in iter_py_files():
            rel = path.relative_to(ROOT).as_posix()
            if path.resolve() == pathlib.Path(__file__).resolve():
                continue  # 本测试自己持有旧名清单
            text = path.read_text(encoding="utf-8", errors="replace")
            for legacy in LEGACY_DIR_NAMES:
                if legacy in text:
                    self.assertIn(
                        rel, LEGACY_FALLBACK_ALLOWLIST,
                        "硬编码旧目录名 %r 且不在 legacy 回退白名单：%s" % (legacy, rel),
                    )


if __name__ == "__main__":
    unittest.main()
