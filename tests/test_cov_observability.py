# -*- coding: utf-8 -*-
"""覆盖率补齐：core/observability.py 的降级分支。

只测「取不到 / 类型不对 / 后端异常」这些路径：空值、坏 __str__、超长字段、
warn 别名、logger 没有对应级别、db/jobs/counts 各种畸形输入。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from core import observability as obs


class _Recorder:
    def __init__(self):
        self.lines = []

    def debug(self, line):
        self.lines.append(("debug", line))

    def info(self, line):
        self.lines.append(("info", line))

    def warning(self, line):
        self.lines.append(("warning", line))

    def error(self, line):
        self.lines.append(("error", line))


class TextHelperTests(unittest.TestCase):
    def test_text_handles_unprintable_and_none(self) -> None:
        class _Bad:
            def __str__(self):
                raise RuntimeError("没有 str")

        self.assertEqual(obs._text(None), "")
        self.assertEqual(obs._text(_Bad()), "<unprintable>")
        self.assertEqual(obs._text("  a \n b  "), "a b")

    def test_empty_values_stay_empty(self) -> None:
        self.assertEqual(obs.mask_token(""), "")
        self.assertEqual(obs.fingerprint(""), "")
        self.assertEqual(obs.scrub(""), "")
        self.assertEqual(obs.mask_value("uid", ""), "")
        self.assertEqual(obs._render_value("uid", ""), '""')

    def test_render_value_truncates_and_quotes(self) -> None:
        long_text = obs._render_value("note", "!" * 300)
        self.assertEqual(len(long_text), obs.MAX_FIELD_CHARS + 3)
        self.assertTrue(long_text.endswith("..."))
        self.assertEqual(obs._render_value("note", 'he said "hi"'), '"he said \'hi\'"')
        self.assertEqual(obs._render_value("note", "plain"), "plain")

    def test_mask_secret_and_masks(self) -> None:
        self.assertEqual(obs.mask_secret(""), "")
        self.assertEqual(obs.mask_secret("abc"), obs.FULL_MASK)
        self.assertEqual(obs.mask_id("1234567890"), "12****90")
        self.assertEqual(obs.mask_open_id("1234567890abcdefghij"), "1234****ghij")


class LogEventTests(unittest.TestCase):
    def test_warn_alias_and_unknown_level(self) -> None:
        recorder = _Recorder()
        with mock.patch.object(obs, "logger", recorder):
            obs.log_event("warn", "evt", uid="12345678")
            obs.log_event("bogus", "evt2")
        self.assertEqual([level for level, _ in recorder.lines], ["warning", "info"])
        self.assertIn("12****78", recorder.lines[0][1])
        self.assertNotIn("12345678", recorder.lines[0][1])

    def test_falls_back_to_info_when_level_missing(self) -> None:
        class _InfoOnly:
            def __init__(self):
                self.lines = []

            def info(self, line):
                self.lines.append(line)

        backend = _InfoOnly()
        with mock.patch.object(obs, "logger", backend):
            line = obs.log_event("debug", "evt")
        self.assertEqual(backend.lines, [line])


class UptimeTests(unittest.TestCase):
    def test_missing_inputs_return_none(self) -> None:
        self.assertIsNone(obs.uptime_seconds(None))
        self.assertIsNone(obs.uptime_seconds(0))
        self.assertIsNone(obs.uptime_seconds(10, None))
        self.assertEqual(obs.uptime_seconds(10, 16.8), 6)


class SnapshotTests(unittest.TestCase):
    def test_plugin_name_prefers_attrs(self) -> None:
        self.assertEqual(obs._plugin_name("直接字符串"), "直接字符串")
        self.assertEqual(obs._plugin_name(SimpleNamespace(PLUGIN_NAME="P")), "P")
        self.assertEqual(obs._plugin_name(SimpleNamespace(plugin_name="q")), "q")
        self.assertEqual(obs._plugin_name(SimpleNamespace(name="n")), "n")
        self.assertEqual(obs._plugin_name(SimpleNamespace()), "")
        self.assertEqual(obs._plugin_attr("str-plugin", "PLUGIN_VERSION"), "")
        self.assertEqual(obs._plugin_attr(SimpleNamespace(PLUGIN_VERSION="2.0"), "PLUGIN_VERSION"), "2.0")

    def test_db_part_dict_and_none(self) -> None:
        self.assertEqual(obs._db_part({"schema_version": 3}),
                         {"schema_version": 3, "ok": True})
        explicit = obs._db_part({"schema_version": 1, "ok": False})
        self.assertFalse(explicit["ok"])
        self.assertEqual(obs._db_part(None), {"schema_version": None, "ok": False})

    def test_db_part_object_probes(self) -> None:
        class _RaisingThenStats:
            def schema_status(self):
                raise RuntimeError("schema 探测炸了")

            def stats(self):
                return {"user_version": 4}

        self.assertEqual(obs._db_part(_RaisingThenStats()),
                         {"schema_version": 4, "ok": True})
        attr_only = SimpleNamespace(schema_version=5)
        self.assertEqual(obs._db_part(attr_only), {"schema_version": 5, "ok": True})

    def test_jobs_part_handles_all_shapes(self) -> None:
        self.assertEqual(obs._jobs_part({"jobs": [{"name": "a"}]}), [{"name": "a"}])
        self.assertEqual(obs._jobs_part({"jobs": "不是列表"}), [])
        self.assertEqual(obs._jobs_part([{"name": "b"}]), [{"name": "b"}])

        class _ReportBoom:
            def report(self):
                raise RuntimeError("report 炸了")

            def summary(self):
                return {"jobs": [{"name": "c"}]}

        self.assertEqual(obs._jobs_part(_ReportBoom()), [{"name": "c"}])
        self.assertEqual(obs._jobs_part([1]), [{"value": "1"}])

    def test_jobs_part_caps_at_fifty(self) -> None:
        rows = obs._jobs_part([{"n": i} for i in range(60)])
        self.assertEqual(len(rows), 50)

    def test_counts_part_converts_values(self) -> None:
        out = obs._counts_part({
            "flag": True, "count": 3, "ratio": 0.123456, "text": "x" * 100,
            "none": None,
        })
        self.assertIs(out["flag"], True)
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["ratio"], 0.123)
        self.assertEqual(len(out["text"]), 60)
        self.assertEqual(out["none"], "")
        self.assertEqual(obs._counts_part("不是字典"), {})

    def test_health_snapshot_degrades_without_db(self) -> None:
        plugin = SimpleNamespace(PLUGIN_NAME="能记", PLUGIN_VERSION="9.9")
        snap = obs.health_snapshot(plugin=plugin, db=None, started_at=100, now=130)
        self.assertEqual(snap["plugin"], "能记")
        self.assertEqual(snap["version"], "9.9")
        self.assertEqual(snap["uptime_s"], 30)
        self.assertEqual(snap["db"], {"schema_version": None, "ok": False})
        self.assertEqual(snap["error_code"], "health.degraded")

        healthy = obs.health_snapshot(plugin="p", version="1", db={"schema_version": 7},
                                      uptime_s=5, counts={"n": 1})
        self.assertEqual(healthy["error_code"], "")
        self.assertEqual(healthy["uptime_s"], 5)


if __name__ == "__main__":
    unittest.main()
