# -*- coding: utf-8 -*-
"""Phase 17：可观测性 / PII 脱敏 / 错误码登记（能记侧）。

钉住三件事：

1. 脱敏单向：mask_* 不含原文、也回不去；
2. 结构化日志里不出现完整 uid / open_id / token / 手机号；
3. api_status 只新增 health 字段，web_response 的 0/1/403 一个都不许变。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import errors as core_errors  # noqa: E402
from core.observability import (  # noqa: E402
    FULL_MASK, fingerprint, health_snapshot, log_event, mask_id, mask_open_id,
    mask_secret, mask_token, mask_value, scrub,
)
from core.web_response import CODE_FAIL, CODE_FORBIDDEN, CODE_OK, fail, ok  # noqa: E402

UID = "123456789"
OPEN_ID = "ABCDEF1234567890XYZ"
TOKEN = "sk-live-abcdefghijklmnop"


class _Recorder:
    """把模块 logger 换成记录器。"""

    def __init__(self):
        self.lines = []

    def __getattr__(self, _name):
        def _emit(*args, **_kwargs):
            self.lines.append(" ".join(str(arg) for arg in args))
        return _emit


class _FakeJobs:
    def report(self):
        return [{"name": "compress", "running": False}]


class MaskMatrixTests(unittest.TestCase):
    def test_mask_id_matrix(self):
        self.assertEqual(mask_id(UID), "12****89")
        self.assertEqual(mask_id("123456"), FULL_MASK)
        self.assertEqual(mask_id(""), "")
        self.assertEqual(mask_id(None), "")

    def test_mask_open_id_token_secret(self):
        self.assertEqual(mask_open_id(OPEN_ID), "ABCD****0XYZ")
        self.assertEqual(mask_token(TOKEN), "****mnop")
        self.assertEqual(mask_token("short"), FULL_MASK)
        self.assertEqual(mask_secret("k-1234567890"), FULL_MASK)

    def test_mask_is_irreversible(self):
        for raw in (UID, OPEN_ID, TOKEN, "13800138000", "1234"):
            masked = mask_value("uid", raw)
            self.assertNotEqual(masked, raw)
            self.assertNotIn(raw, masked)

    def test_fingerprint_is_stable_not_reversible(self):
        first = fingerprint(UID)
        self.assertEqual(first, fingerprint(UID))
        self.assertEqual(len(first), 8)
        self.assertNotIn(UID, first)
        self.assertNotEqual(first, fingerprint("987654321"))


class EventLogTests(unittest.TestCase):
    def setUp(self):
        import core.observability as mod
        self.mod = mod
        self._saved = mod.logger
        self.rec = _Recorder()
        mod.logger = self.rec

    def tearDown(self):
        self.mod.logger = self._saved

    def test_log_event_masks_sensitive_fields(self):
        line = log_event("info", "profile.delete",
                         uid=UID, open_id=OPEN_ID, token=TOKEN, api_key="apikey-123456")
        self.assertTrue(line.startswith("[profile.delete]"), line)
        for raw in (UID, OPEN_ID, TOKEN, "apikey-123456"):
            self.assertNotIn(raw, line)
        self.assertEqual(self.rec.lines, [line])

    def test_log_event_scrubs_free_text(self):
        line = log_event("warning", "chat.raw", detail="对方 QQ 13800138000 说了一句")
        self.assertNotIn("13800138000", line)
        self.assertIn("chat.raw", line)

    def test_scrub_keeps_short_numbers(self):
        self.assertEqual(scrub("code=403 ok"), "code=403 ok")

    def test_log_event_unknown_level_falls_back_to_info(self):
        line = log_event("not-a-level", "weird", a=1)
        self.assertEqual(self.rec.lines, [line])
        self.assertTrue(line.startswith("[weird]"))


class HealthSnapshotTests(unittest.TestCase):
    def test_health_snapshot_shape(self):
        snap = health_snapshot("Astrbot_amoupup_data", version="3.1.0",
                               db={"user_version": 3}, jobs=_FakeJobs(),
                               counts={"profiles": 1},
                               started_at=1000.0, now=1064.0)
        self.assertEqual(snap["plugin"], "Astrbot_amoupup_data")
        self.assertEqual(snap["version"], "3.1.0")
        self.assertEqual(snap["uptime_s"], 64)
        self.assertEqual(snap["db"], {"schema_version": 3, "ok": True})
        self.assertEqual(snap["jobs"][0]["name"], "compress")
        self.assertEqual(snap["counts"], {"profiles": 1})
        self.assertEqual(snap["error_code"], "")

    def test_health_snapshot_degrades_without_raising(self):
        class Broken:
            def schema_status(self):
                raise RuntimeError("db gone")

        snap = health_snapshot("demo", db=Broken(), jobs=object())
        self.assertEqual(snap["db"], {"schema_version": None, "ok": False})
        self.assertEqual(snap["jobs"], [])
        self.assertEqual(snap["error_code"], "health.degraded")

    def test_health_snapshot_accepts_none(self):
        snap = health_snapshot()
        self.assertIsNone(snap["uptime_s"])  # 不读系统时钟，给不出就是 None
        self.assertEqual(snap["db"]["ok"], False)
        self.assertIsNone(snap["db"]["schema_version"])
        self.assertEqual(snap["counts"], {})


class ErrorRegistryTests(unittest.TestCase):
    def test_numeric_codes_unchanged(self):
        self.assertEqual(core_errors.CODE_BY_NUMBER, {0: "ok", 1: "fail", 403: "forbidden"})
        self.assertEqual(core_errors.code_number(core_errors.CODE_OK), 0)
        self.assertEqual(core_errors.code_number(core_errors.CODE_FAIL), 1)
        self.assertEqual(core_errors.code_number(core_errors.CODE_FORBIDDEN), 403)
        self.assertEqual(core_errors.code_number(1), 1)

    def test_describe_code(self):
        self.assertIn("业务失败", core_errors.describe_code(1))
        self.assertIn("守卫拒绝", core_errors.describe_code("forbidden"))
        self.assertIn("降级", core_errors.describe_code(core_errors.CODE_HEALTH_DEGRADED))
        self.assertIn("未知", core_errors.describe_code("no-such-code"))


class ResponseContractTests(unittest.TestCase):
    def test_web_response_numbers_unchanged(self):
        self.assertEqual((CODE_OK, CODE_FAIL, CODE_FORBIDDEN), (0, 1, 403))
        self.assertEqual(ok({"a": 1}), {"code": 0, "data": {"a": 1}})
        self.assertEqual(fail("x")["code"], 1)

    def test_api_status_adds_health_keeps_old_fields(self):
        path = os.path.join(ROOT, "core", "handlers", "system.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        for old in ('"version": PLUGIN_VERSION', '"bot_id": bot_id', '"db": self.db_path',
                    '"counts": counts', '"jobs": self.maintenance.jobs.report()',
                    '"bots": self.db.bot_ids()'):
            self.assertIn(old, source)
        self.assertIn('"health": health_snapshot(', source)


if __name__ == "__main__":
    unittest.main()
