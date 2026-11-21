# -*- coding: utf-8 -*-
"""质量门自己的测试：证明「静默 except 严格化」和「异步阻塞门禁」真的会抓人。

没有这层，门禁很容易变成一句永远绿的空话（历史上就发生过：路由数恒 0、
pyflakes 只扫 main.py）。这里用小样本反例，确认门禁会红。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE_PATH = os.path.join(ROOT, "tools", "quality_gate.py")


def load_gate():
    spec = importlib.util.spec_from_file_location("quality_gate_under_test", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate()
        self._saved_root = self.gate.ROOT
        self.tmp = tempfile.mkdtemp(prefix="gate-policy-")
        self.gate.ROOT = self.tmp

    def tearDown(self):
        self.gate.ROOT = self._saved_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path) or self.tmp, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    def test_bare_silent_except_is_counted(self):
        self.write("bad.py", "try:\n    x = 1\nexcept Exception:\n    pass\n")
        total, spots, marked = self.gate._silent_excepts()
        self.assertEqual(total, 1, spots)
        self.assertEqual(marked, 0)
        self.assertEqual(len(spots), 1)

    def test_cleanup_mark_is_allowed(self):
        self.write("ok.py",
                   "try:\n    x = 1\nexcept OSError:  # cleanup 删临时文件失败无所谓\n    pass\n")
        total, _spots, marked = self.gate._silent_excepts()
        self.assertEqual(total, 0)
        self.assertEqual(marked, 1)

    def test_new_file_with_silent_except_fails_gate(self):
        self.write("news.py", "try:\n    x = 1\nexcept Exception:\n    pass\n")
        with self.assertRaises(AssertionError):
            self.gate.check_silent_except()

    def test_blocking_call_in_async_is_caught(self):
        self.write("bad_async.py",
                   "import time\n\nasync def f():\n    time.sleep(1)\n")
        problems = self.gate._blocking_calls()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("time.sleep", problems[0])

    def test_to_thread_wrapped_call_is_ok(self):
        self.write("ok_async.py",
                   "import asyncio\nimport time\n\nasync def f():\n"
                   "    await asyncio.to_thread(time.sleep, 1)\n")
        self.assertEqual(self.gate._blocking_calls(), [])

    def test_block_io_mark_is_allowed(self):
        self.write("marked.py",
                   "import time\n\nasync def f():\n"
                   "    time.sleep(1)  # block-io-ok 故意演示\n")
        self.assertEqual(self.gate._blocking_calls(), [])

    def test_sync_function_is_not_flagged(self):
        self.write("sync.py", "import time\n\ndef f():\n    time.sleep(1)\n")
        self.assertEqual(self.gate._blocking_calls(), [])


if __name__ == "__main__":
    unittest.main()
