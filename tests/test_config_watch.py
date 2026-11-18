# -*- coding: utf-8 -*-
"""utils/config_watch.py：热更新、拒绝路径穿越/超大/非对象、解析失败保留旧配置。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config import PluginSettings  # noqa: E402
from utils.config_watch import ConfigWatcher  # noqa: E402


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "Astrbot_test_config.json")
        self._write({"recall_graph_route": True, "chain_trace": True})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, payload):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.utime(self.path, None)

    def _watcher(self, **kwargs):
        options = {"allowed_dir": self.tmp, "min_interval": 0.0}
        options.update(kwargs)
        return ConfigWatcher(self.path, **options)

    def test_initial_load(self):
        watcher = self._watcher()
        self.assertTrue(watcher.enabled)
        self.assertTrue(watcher.get()["chain_trace"])

    def test_reload_after_change(self):
        watcher = self._watcher()
        self._write({"recall_graph_route": False})
        self.assertTrue(watcher.maybe_reload())
        self.assertFalse(watcher.get()["recall_graph_route"])

    def test_unchanged_file_is_skipped(self):
        watcher = self._watcher()
        self.assertFalse(watcher.maybe_reload())

    def test_invalid_json_keeps_old(self):
        watcher = self._watcher()
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertFalse(watcher.maybe_reload())
        self.assertTrue(watcher.get()["chain_trace"], "解析失败必须保留旧配置")
        self.assertTrue(watcher.last_error)

    def test_non_object_rejected(self):
        watcher = self._watcher()
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("[1, 2]")
        self.assertFalse(watcher.maybe_reload())
        self.assertEqual(watcher.get()["chain_trace"], True)

    def test_path_outside_allowed_dir_rejected(self):
        outside = tempfile.mkdtemp()
        try:
            path = os.path.join(outside, "Astrbot_test_config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"x": 1}, fh)
            watcher = ConfigWatcher(path, allowed_dir=self.tmp)
            self.assertFalse(watcher.enabled)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_wrong_suffix_rejected(self):
        path = os.path.join(self.tmp, "secrets.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"x": 1}, fh)
        self.assertFalse(ConfigWatcher(path, allowed_dir=self.tmp).enabled)

    def test_oversize_rejected(self):
        watcher = self._watcher(max_bytes=1024)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"big": "x" * 5000}, fh)
        self.assertFalse(watcher.maybe_reload())
        self.assertTrue(watcher.get()["chain_trace"])

    def test_settings_reload_in_place(self):
        settings = PluginSettings({"recall_graph_route": True, "chain_trace": True})
        self.assertTrue(settings.get("recall_graph_route"))
        settings.reload({"recall_graph_route": False, "chain_trace": False})
        self.assertFalse(settings.get("recall_graph_route"))
        self.assertFalse(settings.get("chain_trace"))


if __name__ == "__main__":
    unittest.main()
