# -*- coding: utf-8 -*-
"""Phase 18 测试基建防回退：stub/support 一致、临时目录治理、fixtures 迁移、仿真复用。

这些用例只读文件与 sys.modules，不碰生产库；任一约定被改坏（三份不一致 /
stub 少名字 / conftest 不清理 / 脚本绕过 sim_lib）都会在这里红。
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(ROOT)
PLUGIN_NAMES = ("Astrbot_amoupup_forever", "Astrbot_amoupup_data", "Astrbot_amoupup_watch")

_MISSING_SIBLINGS = [n for n in PLUGIN_NAMES if not os.path.isdir(os.path.join(PLUGINS, n))]
if _MISSING_SIBLINGS:
    raise unittest.SkipTest("缺少同级插件仓库（GitHub 只 checkout 当前仓库）: %s" % _MISSING_SIBLINGS)

if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

LEGACY_NAMES = (".test-tmp", "_test_tmp")
#: 冻结基线：这些文件在 Phase 18 之前就用真实 datetime.now() 造数据；只许减不许增。
DATETIME_NOW_ALLOW = {
    "Astrbot_amoupup_forever": {"test_fact_binder.py", "test_intent_wiring.py",
                                "test_panel_doing.py", "test_round8.py", "test_clock.py",
                                "test_source_guard.py"},
    "Astrbot_amoupup_data": {"test_clock.py"},
    "Astrbot_amoupup_watch": {"test_deposit_decision.py"},
}
#: 三个文件必须逐字节一致。
CANONICAL_FILES = ("tests/astrbot_stub.py", "tests/support/__init__.py",
                   "tests/support/fakes.py", "tests/conftest.py")


def _plugin_name():
    return os.path.basename(ROOT)


def _read(plugin, rel):
    with open(os.path.join(PLUGINS, plugin, rel), "rb") as handle:
        return handle.read()


def _sha(plugin, rel):
    return hashlib.sha256(_read(plugin, rel)).hexdigest()


def _bare_datetime_now(path):
    """AST 找真正的 datetime/dt.datetime.now() 调用（不含注释/字符串）。"""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    hits = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "now":
            continue
        base = func.value
        name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
        if name in ("datetime", "_dt", "dt"):
            hits += 1
    return hits


class StubConsistencyTests(unittest.TestCase):
    def test_stub_sha256_identical_across_three(self):
        shas = {plugin: _sha(plugin, "tests/astrbot_stub.py") for plugin in PLUGIN_NAMES}
        self.assertEqual(len(set(shas.values())), 1, shas)

    def test_stub_version_identical(self):
        stub = importlib.import_module("astrbot_stub")
        versions = {stub.STUB_VERSION}
        for plugin in PLUGIN_NAMES:
            body = _read(plugin, "tests/astrbot_stub.py").decode("utf-8")
            self.assertIn('STUB_VERSION = "%s"' % versions.pop(), body)
            versions.add(stub.STUB_VERSION)
        self.assertEqual(stub.STUB_VERSION, "1.0")

    def test_stub_installs_required_symbols(self):
        stub = importlib.import_module("astrbot_stub")
        stub.install()
        for name in stub.REQUIRED_MODULES:
            self.assertIn(name, sys.modules, "stub 缺少模块 " + name)
        components = sys.modules["astrbot.api.message_components"]
        for attr in ("Plain", "Reply", "At", "Record", "Image"):
            self.assertTrue(hasattr(components, attr), "stub 缺少组件 " + attr)
        self.assertTrue(stub.is_installed())


class SupportConsistencyTests(unittest.TestCase):
    def test_support_sha256_identical_across_three(self):
        for rel in ("tests/support/__init__.py", "tests/support/fakes.py", "tests/conftest.py"):
            shas = {plugin: _sha(plugin, rel) for plugin in PLUGIN_NAMES}
            self.assertEqual(len(set(shas.values())), 1, (rel, shas))

    def test_support_version_and_helpers(self):
        support = importlib.import_module("support")
        self.assertEqual(support.SUPPORT_VERSION, "1.0")
        for name in ("FakeClock", "temp_dir", "temp_store", "LogCapture", "assert_no_pii"):
            self.assertTrue(hasattr(support, name), "support 缺少 " + name)

    def test_fixtures_reexport_support(self):
        fixtures = importlib.import_module("fixtures")
        self.assertEqual(fixtures.SUPPORT_VERSION, "1.0")
        for name in ("FakeClock", "temp_dir", "assert_no_pii"):
            self.assertTrue(hasattr(fixtures, name), "fixtures 未 re-export " + name)


class TempDirGovernanceTests(unittest.TestCase):
    def test_conftest_points_tempdir_to_mkdtemp(self):
        body = _read(_plugin_name(), "tests/conftest.py").decode("utf-8")
        self.assertIn("_ORIG_MKDTEMP(prefix=", body)
        self.assertIn("tempfile.tempdir = _TEMP_ROOT", body)
        self.assertIn("cleanup_session_temp_root", body)
        self.assertNotIn("tempfile.tempdir = os.path.join", body)

    def test_no_legacy_temp_dirs_after_run(self):
        support = importlib.import_module("support")
        support.assert_no_legacy_temp_dirs(ROOT)
        for name in LEGACY_NAMES:
            self.assertFalse(os.path.exists(os.path.join(ROOT, name)), name)

    def test_runtime_tempdir_is_outside_repo(self):
        current = os.path.abspath(tempfile.gettempdir())
        repo_local = [os.path.abspath(os.path.join(ROOT, name)) for name in LEGACY_NAMES]
        self.assertNotIn(current, repo_local)
        self.assertTrue(os.path.isdir(current))

    def test_mkdtemp_stays_inside_session_root(self):
        path = tempfile.mkdtemp(prefix="amo-infra-probe-")
        try:
            self.assertTrue(os.path.isdir(path))
            self.assertTrue(os.path.abspath(path).startswith(os.path.abspath(tempfile.gettempdir())))
        finally:
            os.rmdir(path)


class MigrationAndSimTests(unittest.TestCase):
    def test_shared_fake_importers_at_least_eight(self):
        import re
        pattern = re.compile(r"from (support|plugin_fakes|fixtures|tests\.social_fakes|tests\.fixtures) import")
        count = 0
        for name in sorted(os.listdir(TESTS)):
            if name.startswith("test_") and name.endswith(".py"):
                with open(os.path.join(TESTS, name), encoding="utf-8") as handle:
                    if pattern.search(handle.read()):
                        count += 1
        self.assertGreaterEqual(count, 8, "迁移到共享 Fake* 的测试文件不足 8 个：%d" % count)

    def test_no_new_bare_datetime_now(self):
        allowed = DATETIME_NOW_ALLOW.get(_plugin_name(), set())
        offenders = []
        for name in sorted(os.listdir(TESTS)):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            if name in allowed:
                continue
            if _bare_datetime_now(os.path.join(TESTS, name)):
                offenders.append(name)
        self.assertFalse(offenders, "新增裸 datetime.now()：%s" % offenders)

    def test_work_scripts_go_through_sim_lib(self):
        work = os.path.join(ROOT, "work")
        if not os.path.isdir(work):
            return
        self.assertTrue(os.path.isfile(os.path.join(work, "sim_lib.py")))
        lib = importlib.import_module("sim_lib") if work in sys.path else None
        if lib is None:
            sys.path.insert(0, work)
            lib = importlib.import_module("sim_lib")
        for name in ("install_stub", "FakeDB", "FakePlugin", "set_clock", "restore_clock"):
            self.assertTrue(hasattr(lib, name), "sim_lib 缺少 " + name)
        for script in ("simulate_day.py", "simulate_24h.py", "simulate_weeks.py"):
            with open(os.path.join(work, script), encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=script)
            modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    modules.add(node.module or "")
            self.assertIn("sim_lib", modules, script + " 没有 import sim_lib")


if __name__ == "__main__":
    unittest.main()
