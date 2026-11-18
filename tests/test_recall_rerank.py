# -*- coding: utf-8 -*-
"""二阶段重排：解析、重排、以及「任何失败都原样返回」的兜底。"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import recall_rerank  # noqa: E402


def _items():
    return [{"id": 1, "content": "甲"}, {"id": 2, "content": "乙"}, {"id": 3, "content": "丙"}]


class ParseTests(unittest.TestCase):
    def test_json_ids(self):
        self.assertEqual(recall_rerank.parse_ids('{"ids": [2, 1]}', [1, 2, 3], 2), [2, 1])

    def test_only_valid_and_dedup_and_cap(self):
        self.assertEqual(
            recall_rerank.parse_ids('{"ids": [2, 2, 99, 1, 3]}', [1, 2, 3], 2), [2, 1])

    def test_fallback_numbers(self):
        self.assertEqual(recall_rerank.parse_ids("我觉得 3 和 1 更好", [1, 2, 3], 2), [3, 1])

    def test_garbage_is_empty(self):
        self.assertEqual(recall_rerank.parse_ids("没有 id", [1, 2], 2), [])


class PromptTests(unittest.TestCase):
    def test_prompt_has_ids_and_query(self):
        prompt = recall_rerank.build_prompt("团子呢", _items(), 2, snippets=8)
        self.assertIn("id=1", prompt)
        self.assertIn("团子呢", prompt)
        self.assertIn('{"ids"', prompt)


class RerankTests(unittest.TestCase):
    def _run(self, call, limit=2, **kwargs):
        return asyncio.run(recall_rerank.rerank(_items(), "团子呢", limit, call, **kwargs))

    def test_reorders_by_model_ids(self):
        async def call(prompt):
            return '{"ids": [3, 1]}'

        rows = self._run(call)
        self.assertEqual([row["id"] for row in rows], [3, 1],
                         "模型没选的候选允许被丢掉（宁缺毋滥）")

    def test_failure_keeps_original(self):
        async def call(prompt):
            raise RuntimeError("boom")

        self.assertEqual([r["id"] for r in self._run(call)], [1, 2, 3])

    def test_empty_ids_keeps_original(self):
        async def call(prompt):
            return '{"ids": []}'

        self.assertEqual([r["id"] for r in self._run(call)], [1, 2, 3])

    def test_timeout_keeps_original(self):
        async def call(prompt):
            await asyncio.sleep(0.2)
            return '{"ids": [2]}'

        self.assertEqual([r["id"] for r in self._run(call, timeout=0.05)], [1, 2, 3])

    def test_single_item_never_calls(self):
        called = []

        async def call(prompt):
            called.append(prompt)
            return '{"ids": [1]}'

        rows = asyncio.run(recall_rerank.rerank([{"id": 1, "content": "甲"}], "q", 2, call))
        self.assertEqual(called, [])
        self.assertEqual(rows, [{"id": 1, "content": "甲"}])


if __name__ == "__main__":
    unittest.main()
