# -*- coding: utf-8 -*-
"""Phase 18 跨插件共享测试支持：三插件逐字节一致（sha256 由 test_test_infra 校验）。

集中放置假时钟 / 临时目录与临时库 / 日志捕获 / PII 断言 / 遗留临时目录治理。
插件自己的 fixtures.py 只做 re-export + 本插件特化，不再各抄一份。
"""
from __future__ import annotations

import atexit
import contextlib
import hashlib
import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta

SUPPORT_VERSION = "1.0"
LEGACY_TMP_NAMES = (".test-tmp", "_test_tmp")
DEFAULT_NOW = datetime(2026, 11, 16, 15, 0, 0)

_CREATED_DIRS: list[str] = []


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def legacy_tmp_paths(root):
    return [os.path.join(str(root), name) for name in LEGACY_TMP_NAMES]


def assert_no_legacy_temp_dirs(root):
    found = [path for path in legacy_tmp_paths(root) if os.path.exists(path)]
    if found:
        raise AssertionError("测试跑完还残留旧临时目录：%s" % found)
    return True


def cleanup_legacy_temp_dirs(root):
    removed = []
    for path in legacy_tmp_paths(root):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)
    return removed


class FakeClock:
    """可手动推进的假时钟（不 patch 全局）：now/now_ts/day/advance，可当上下文。"""

    def __init__(self, moment=None):
        self.moment = moment or DEFAULT_NOW

    def now(self):
        return self.moment

    def now_ts(self):
        return self.moment.timestamp()

    def today(self):
        return self.moment.date()

    def day(self):
        return self.moment.date().isoformat()

    def __call__(self, *args, **kwargs):
        return self.moment

    def set(self, moment):
        self.moment = moment
        return self

    def advance(self, seconds):
        self.moment = self.moment + timedelta(seconds=float(seconds))
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class LogCapture:
    """抓取 logging 记录，供断言日志里有/没有敏感信息。"""

    def __init__(self, logger=None, level=logging.DEBUG):
        self.logger = logger or logging.getLogger()
        self.level = level
        self.records = []
        self._handler = None
        self._old_level = None

    def start(self):
        self._old_level = self.logger.level
        self.logger.setLevel(min(self.level, self._old_level or self.level))
        self._handler = _ListHandler(self.records)
        self._handler.setLevel(self.level)
        self.logger.addHandler(self._handler)
        return self

    def stop(self):
        if self._handler is not None:
            self.logger.removeHandler(self._handler)
            self._handler = None
        if self._old_level is not None:
            self.logger.setLevel(self._old_level)
            self._old_level = None
        return self

    @property
    def text(self):
        return "\n".join(record.getMessage() for record in self.records)

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False


class _ListHandler(logging.Handler):
    def __init__(self, records):
        super().__init__()
        self.records = records

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def temp_dir(prefix="amo-test-"):
    """一次性临时目录：退出即删（优先落在 conftest 指定的临时根下）。"""
    path = tempfile.mkdtemp(prefix=prefix)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextlib.contextmanager
def temp_store(name="amo-test.db", prefix="amo-store-"):
    """一次性 SQLite 路径：退出时连同目录一起删；不碰 plugin_data。"""
    with temp_dir(prefix=prefix) as root:
        yield os.path.join(root, name)


def make_temp_dir(prefix="amo-test-"):
    """建一个退出时统一删除的临时目录（给需要跨 with 的旧测试用）。"""
    path = tempfile.mkdtemp(prefix=prefix)
    _CREATED_DIRS.append(path)
    return path


@atexit.register
def _cleanup_created_dirs():
    while _CREATED_DIRS:
        shutil.rmtree(_CREATED_DIRS.pop(), ignore_errors=True)


def assert_no_pii(text, secrets):
    """断言文本里没有出现任何敏感串（手机号/QQ/真名等由调用方给出）。"""
    leaked = [str(item) for item in secrets if item and str(item) in str(text)]
    if leaked:
        raise AssertionError("日志出现 PII：%s" % leaked)
    return True


assert_redacted = assert_no_pii


def connect(path):
    """测试专用 SQLite 连接（短超时，便于暴露锁冲突）。"""
    return sqlite3.connect(str(path), timeout=5.0)


__all__ = [
    "SUPPORT_VERSION", "LEGACY_TMP_NAMES", "DEFAULT_NOW", "FakeClock", "LogCapture",
    "temp_dir", "temp_store", "make_temp_dir", "sha256_file", "sha256_text",
    "sha256_bytes", "legacy_tmp_paths", "assert_no_legacy_temp_dirs",
    "cleanup_legacy_temp_dirs", "assert_no_pii", "assert_redacted", "connect",
]
