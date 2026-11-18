# -*- coding: utf-8 -*-
"""覆盖率补齐：utils/text_process.py 的空值/坏输入分支。"""
from __future__ import annotations

import struct
import unittest
from unittest import mock

from utils import text_process as tp


class PlatformTests(unittest.TestCase):
    def test_empty_umo_returns_empty(self) -> None:
        self.assertEqual(tp.platform_of_umo(""), "")
        self.assertEqual(tp.platform_of_umo(None), "")


class FingerprintTests(unittest.TestCase):
    def test_empty_text_has_no_fingerprint(self) -> None:
        self.assertEqual(tp._norm_key(""), "")
        self.assertEqual(tp._norm_key("   ，。 "), "")


class InformativeTermsTests(unittest.TestCase):
    def test_english_only_returns_words(self) -> None:
        self.assertEqual(tp._informative_terms("hello world"), ["hello", "world"])

    def test_single_char_query_returns_char(self) -> None:
        terms = tp._informative_terms("猫")
        self.assertEqual(terms, ["猫"])


class ScoreTests(unittest.TestCase):
    def test_jaccard_empty_side_is_zero(self) -> None:
        self.assertEqual(tp._jaccard([], ["a"]), 0.0)
        self.assertEqual(tp._jaccard(["a"], []), 0.0)

    def test_cosine_guards_and_value(self) -> None:
        self.assertEqual(tp._cosine([], [1.0]), 0.0)
        self.assertEqual(tp._cosine([1.0], [1.0, 2.0]), 0.0)
        self.assertEqual(tp._cosine([0.0, 0.0], [0.0, 0.0]), 0.0)
        self.assertAlmostEqual(tp._cosine([1.0, 2.0], [2.0, 4.0]), 1.0)

    def test_bm25_empty_inputs(self) -> None:
        self.assertEqual(tp._bm25_scores([], []), [])
        self.assertEqual(tp._bm25_scores([], [["a"], ["b"]]), [0.0, 0.0])


class SlotTests(unittest.TestCase):
    def test_fact_with_glue_chars_is_dropped(self) -> None:
        self.assertEqual(tp._slot_facts("住在这样的地方"), {})
        self.assertEqual(tp._slot_facts(""), {})
        self.assertEqual(tp._slot_facts("住在杭州")["home"], "杭州")


class VectorTests(unittest.TestCase):
    def test_bad_blob_returns_empty(self) -> None:
        class _BadBytes:
            def __bool__(self):
                return True

            def __bytes__(self):
                raise RuntimeError("坏对象")

        self.assertEqual(tp._to_vec(_BadBytes(), 2), [])
        self.assertEqual(tp._to_vec(b"", 2), [])
        self.assertEqual(tp._to_vec(b"1234", 0), [])

    def test_unpack_failure_returns_empty(self) -> None:
        with mock.patch.object(tp.struct, "unpack", side_effect=struct.error("坏数据")):
            self.assertEqual(tp._to_vec(b"12345678", 2), [])


class FreshnessTests(unittest.TestCase):
    def test_bad_timestamp_defaults_to_fresh(self) -> None:
        self.assertEqual(tp._freshness("不是数字", 1000, 45), 1.0)
        self.assertEqual(tp._freshness(None, 1000, 45), 1.0)
        self.assertLess(tp._freshness(1000, 1000 + 45 * 86400, 45), 1.0)


if __name__ == "__main__":
    unittest.main()
