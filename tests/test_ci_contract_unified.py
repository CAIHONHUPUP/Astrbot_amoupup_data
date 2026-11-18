# -*- coding: utf-8 -*-
"""统一 CI 契约：.github/workflows/test.yml 必须守同一套步骤。

只用标准库做文本断言，不依赖 PyYAML / 网络 / 真实 DB；
四仓库共用同一份 workflow，任何一份漂移都会在这里变红。
"""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"
PYTEST_COMMAND = "coverage run --rcfile=.coveragerc -m pytest tests -q -p no:cacheprovider"


class UnifiedWorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_python_and_timezone_are_pinned(self):
        self.assertIn('python-version: "3.12"', self.text)
        self.assertIn("TZ: Asia/Shanghai", self.text)

    def test_installs_dev_requirements(self):
        self.assertIn("pip install -r requirements-dev.txt", self.text)

    def test_single_pytest_command_and_coverage_floor(self):
        self.assertEqual(self.text.count("pytest tests"), 1, "只允许一条统一 pytest 命令")
        self.assertIn(PYTEST_COMMAND, self.text)
        self.assertIn("coverage report --rcfile=.coveragerc", self.text)

    def test_failures_turn_red(self):
        self.assertNotIn("continue-on-error", self.text, "CI 不允许吞错")

    def test_optional_gates_are_guarded(self):
        # 有工具就跑、没有就明确 SKIP：四仓库才能共用同一份 workflow。
        self.assertIn("tools/check_version.py", self.text)
        self.assertIn("tools/quality_gate.py --fast", self.text)
        self.assertIn("SKIP:", self.text)

    def test_triggers_main_branch(self):
        self.assertIn("branches: [main]", self.text)
        self.assertIn("pull_request:", self.text)


if __name__ == "__main__":
    unittest.main()
