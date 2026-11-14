# -*- coding: utf-8 -*-
"""上架审核的两条硬规矩，用源码守卫盯着，别再回退。"""
import glob
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


class LoggingRuleTests(unittest.TestCase):
    def test_store_does_not_fallback_to_builtin_logging(self):
        names = ["store.py", "models/memory_db.py", "core/api.py"]
        names += [p.replace(ROOT + os.sep, "").replace("\\", "/")
                  for p in glob.glob(os.path.join(ROOT, "services", "*.py"))]
        for name in names:
            body = read(name)
            self.assertNotIn("import logging", body, name)
            self.assertNotIn("logging.getLogger", body, name)
        joined = "\n".join(read(name) for name in names)
        self.assertIn("from astrbot.api import logger", joined)


class AsyncBlockingTests(unittest.TestCase):
    def test_api_memories_offloads_recall(self):
        body = read("core/web_handlers.py")
        start = body.index("async def api_memories")
        end = body.index("async def api_memory_save", start)
        segment = body[start:end]
        self.assertIn("asyncio.to_thread", segment)
        self.assertNotIn("self.store.recall(", segment)

    def test_memory_api_recall_offloads(self):
        body = read("core/api.py")
        start = body.index("async def recall", body.index("class MemoryAPI"))
        segment = body[start:start + 900]
        self.assertIn("asyncio.to_thread", segment)


if __name__ == "__main__":
    unittest.main()
