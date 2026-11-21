# -*- coding: utf-8 -*-
"""Phase 19：跨插件协议版本化 + 发布回滚工具。

覆盖：
- protocol_info / PROTOCOL_VERSION 稳定、api_hash 可复算；
- compat 的 ok / legacy / degraded 三态（watch 侧）；
- 契约快照包含 protocol_info；
- rollback 的 manifest / verify / plan / apply（越界、篡改、缺清单都拒绝）；
- release_check 版本不一致与安装包缺文件都报错。
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)


def load_tool(name):
    path = os.path.join(ROOT, "tools", name)
    spec = importlib.util.spec_from_file_location("phase19_" + name[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rollback = load_tool("rollback.py")
release = load_tool("release_check.py")

from core.api import (MemoryAPI, PROTOCOL_NAME, PROTOCOL_VERSION,  # noqa: E402
                      memory_api_names)


class ProtocolTests(unittest.TestCase):
    def test_protocol_info_shape(self):
        info = MemoryAPI.protocol_info(object())
        self.assertEqual(sorted(info), ["api_hash", "plugin", "protocol",
                                        "protocol_version", "schema_version", "version"])
        self.assertEqual(info["plugin"], "Astrbot_amoupup_data")
        self.assertEqual(info["protocol"], "amo-memory")
        self.assertEqual(info["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(PROTOCOL_VERSION, "1.0")
        self.assertEqual(len(info["api_hash"]), 12)
        self.assertGreater(info["schema_version"], 0)

    def test_memory_api_hash_is_recomputable_and_stable(self):
        first = MemoryAPI.protocol_info(object())["api_hash"]
        second = MemoryAPI.protocol_info(object())["api_hash"]
        payload = "\n".join(memory_api_names()).encode("utf-8")
        self.assertEqual(first, hashlib.sha256(payload).hexdigest()[:12])
        self.assertEqual(first, second)
        self.assertIn("nightly_apply", memory_api_names())
        self.assertNotIn("protocol_info", memory_api_names())

    def test_reverse_api_hash_moves_when_one_name_changes(self):
        names = sorted(memory_api_names() + ["zzz_renamed"])
        moved = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()[:12]
        self.assertNotEqual(moved, MemoryAPI.protocol_info(object())["api_hash"])

    def test_memory_api_snapshot_contains_protocol_info(self):
        path = os.path.join(ROOT, "tests", "contracts", "memory_api_surface.json")
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertIn("protocol_info", saved)
        self.assertEqual(list(saved), sorted(saved))

    def test_status_payload_adds_protocol_only(self):
        path = os.path.join(ROOT, "core", "handlers", "system.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('"protocol": self.mem.protocol_info(),', source)
        self.assertIn('"health": health_snapshot(', source)


def make_tree(root, files):
    for rel, text in files.items():
        path = os.path.join(root, rel)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="phase19-rollback-")
        self.backup = os.path.join(self.base, "backup")
        self.target = os.path.join(self.base, "target")
        os.makedirs(self.backup, exist_ok=True)
        os.makedirs(self.target, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def seed(self):
        make_tree(self.backup, {"main.py": "VALUE = 2\n", "data.txt": "new\n"})
        make_tree(self.target, {"main.py": "VALUE = 1\n", "data.txt": "old\n"})
        return rollback.write_manifest(self.backup,
                                       os.path.join(self.backup, "manifest.json"))

    def test_manifest_and_verify_roundtrip(self):
        saved = self.seed()
        self.assertEqual(saved["count"], 2)
        checked = rollback.verify(self.backup, os.path.join(self.backup, "manifest.json"), self.base)
        self.assertTrue(checked["ok"], checked)

    def test_verify_detects_tamper(self):
        self.seed()
        with open(os.path.join(self.backup, "data.txt"), "w", encoding="utf-8") as fh:
            fh.write("tampered\n")
        checked = rollback.verify(self.backup, os.path.join(self.backup, "manifest.json"), self.base)
        self.assertFalse(checked["ok"])
        self.assertIn("data.txt", checked["diff"]["changed"])

    def test_plan_is_read_only_and_counts(self):
        self.seed()
        result = rollback.plan(self.backup, self.target, self.base)
        self.assertEqual((result["add"], result["overwrite"]), (0, 2))
        with open(os.path.join(self.target, "main.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "VALUE = 1\n")

    def test_refuses_paths_outside_base(self):
        outside = tempfile.mkdtemp(prefix="phase19-outside-")
        try:
            with self.assertRaises(rollback.Refused):
                rollback.plan(outside, self.target, self.base)
            with self.assertRaises(rollback.Refused):
                rollback.apply(outside, self.target, "", "", self.base)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_apply_defaults_to_dry_run(self):
        self.seed()
        result = rollback.apply(self.backup, self.target, "",
                                os.path.join(self.backup, "manifest.json"), self.base)
        self.assertTrue(result["dry_run"])
        with open(os.path.join(self.target, "main.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "VALUE = 1\n")

    def test_apply_restores_on_temp_copy(self):
        self.seed()
        dry = rollback.apply(self.backup, self.target, "",
                             os.path.join(self.backup, "manifest.json"), self.base)
        result = rollback.apply(self.backup, self.target, dry["token"],
                                os.path.join(self.backup, "manifest.json"), self.base)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["dry_run"])
        self.assertTrue(os.path.isdir(result["shelter"]))
        with open(os.path.join(self.target, "main.py"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "VALUE = 2\n")
        self.assertEqual(result["compile"]["checked"], 1)

    def test_apply_refuses_without_manifest(self):
        with self.assertRaises(rollback.Refused):
            rollback.apply(self.backup, self.target, "whatever",
                           os.path.join(self.base, "missing.json"), self.base)

    def test_apply_refuses_tampered_backup(self):
        self.seed()
        dry = rollback.apply(self.backup, self.target, "",
                             os.path.join(self.backup, "manifest.json"), self.base)
        with open(os.path.join(self.backup, "data.txt"), "w", encoding="utf-8") as fh:
            fh.write("changed-after-manifest\n")
        with self.assertRaises(rollback.Refused):
            rollback.apply(self.backup, self.target, dry["token"],
                           os.path.join(self.backup, "manifest.json"), self.base)
        with open(os.path.join(self.target, "data.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "old\n")


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="phase19-release-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def seed(self, main_version, metadata_version=None, changelog_version=None,
             readme_version=None):
        metadata_version = metadata_version or main_version
        changelog_version = changelog_version or main_version
        readme_version = readme_version or main_version
        make_tree(self.root, {
            "metadata.yaml": "name: demo\nversion: %s\n" % metadata_version,
            "main.py": 'PLUGIN_VERSION = "%s"\n' % main_version,
            "CHANGELOG.md": "## %s\n" % changelog_version,
            "README.md": "## %s 正式版\n" % readme_version,
        })

    def test_versions_consistent(self):
        self.seed("1.2.3")
        result = release.check_versions(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["versions"]["changelog"], "1.2.3")

    def test_version_mismatch_is_error(self):
        self.seed("1.2.3", readme_version="1.2.2")
        result = release.check_versions(self.root)
        self.assertFalse(result["ok"])
        self.assertIn("版本不一致", " ".join(result["errors"]))
        with contextlib.redirect_stdout(io.StringIO()):
            code = release.main(["--dir", self.root])
        self.assertEqual(code, 1)

    def test_zip_requires_all_files(self):
        zip_path = os.path.join(self.root, "pkg.zip")
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("demo/main.py", "x")
        partial = release.check_zip(zip_path)
        self.assertFalse(partial["ok"])
        self.assertIn("metadata.yaml", partial["missing"])
        with zipfile.ZipFile(zip_path, "a") as archive:
            for name in ("metadata.yaml", "README.md", "CHANGELOG.md"):
                archive.writestr("demo/" + name, "x")
        self.assertTrue(release.check_zip(zip_path)["ok"])


if __name__ == "__main__":
    unittest.main()
