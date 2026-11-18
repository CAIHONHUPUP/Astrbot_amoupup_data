# -*- coding: utf-8 -*-
"""services/chain.py：默认开、可关、detail 去换行、done 总耗时。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import chain  # noqa: E402
from support.store_fixture import DataStore  # noqa: E402

BOT = "bot1"
UID = "302578963"


class _Sink:
    def __init__(self):
        self.lines = []

    def info(self, fmt, *args):
        self.lines.append(fmt % args if args else str(fmt))

    def warning(self, fmt, *args):
        self.lines.append(fmt % args if args else str(fmt))


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.sink = _Sink()
        self.old = chain.logger
        chain.logger = self.sink

    def tearDown(self):
        chain.logger = self.old

    def test_emits_steps_and_done(self):
        trace = chain.new_chain("recall", True)
        trace.step("substr", "n=3")
        trace.done("n=1")
        self.assertTrue(any("[chain] recall" in line for line in self.sink.lines))
        self.assertTrue(any("step=substr" in line for line in self.sink.lines))
        self.assertTrue(any("done" in line for line in self.sink.lines))

    def test_disabled_is_silent(self):
        trace = chain.new_chain("recall", False)
        trace.step("substr")
        trace.fail("boom", "x")
        trace.done()
        self.assertEqual(self.sink.lines, [])

    def test_detail_cannot_inject_newline(self):
        trace = chain.new_chain("write", True)
        trace.step("bad", "a\nb\r\nc")
        line = self.sink.lines[-1]
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertIn("a b  c", line)

    def test_fail_is_warning(self):
        trace = chain.new_chain("write", True)
        trace.fail("prepare", "empty")
        self.assertIn("FAIL:empty", self.sink.lines[-1])


class RecallTraceTests(unittest.TestCase):
    def setUp(self):
        self.sink = _Sink()
        self.old = chain.logger
        chain.logger = self.sink
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.store = DataStore(os.path.join(self.tmp, "amo.db"), settings={
            "embedding_enabled": False, "chain_trace": True,
        })
        self.store.remember(BOT, UID, "主人养了一只叫团子的猫", kind="fact")

    def tearDown(self):
        chain.logger = self.old
        self.store.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recall_emits_chain(self):
        self.store.recall(BOT, UID, "团子")
        joined = "\n".join(self.sink.lines)
        self.assertIn("[chain] recall", joined)
        self.assertIn("step=entity", joined)

    def test_recall_silent_when_off(self):
        self.store.settings["chain_trace"] = False
        self.store.recall(BOT, UID, "团子")
        self.assertNotIn("[chain] recall", "\n".join(self.sink.lines))


if __name__ == "__main__":
    unittest.main()
