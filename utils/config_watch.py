# -*- coding: utf-8 -*-
"""utils/config_watch.py —— 配置热更新（改完配置不用重启插件）。

只做三件事：stat 比对 mtime/size、按上限读文件、json 解析成 dict。规则：
* 路径必须是 `data/config/<插件>_config.json`、且落在允许目录内（realpath 防 .. / 软链穿越）；
* 只读、不写；文件超过 1 MiB 直接拒绝；解析失败保留旧配置（绝不把插件打成半配置）；
* 最小检查间隔 1s，避免每次请求都 stat。

安全：配置里可能有 api key，错误日志只报「第几个字节/什么错」，不打印内容。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional

try:
    from ..utils.text_process import logger
except ImportError:  # pragma: no cover - 测试脚本按文件加载时
    try:
        from astrbot.api import logger
    except ImportError:

        class _NullLogger:
            def info(self, *_args, **_kwargs):
                return None

            def warning(self, *_args, **_kwargs):
                return None

        logger = _NullLogger()

__all__ = ["ConfigWatcher"]

MAX_BYTES = 1 << 20


class ConfigWatcher:
    def __init__(self, path: str, allowed_dir: Optional[str] = None,
                 max_bytes: int = MAX_BYTES, min_interval: float = 1.0):
        self.max_bytes = max(1024, int(max_bytes))
        self.interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        self._mtime_ns = -1
        self._size = -1
        self._next_check = 0.0
        self.reload_count = 0
        self.last_error = ""
        self.path = self._resolve(path, allowed_dir)
        self.enabled = self.path is not None
        if self.enabled:
            self._read(initial=True)

    # ---------- 路径安全 ----------
    @staticmethod
    def _resolve(path: Optional[str], allowed_dir: Optional[str]) -> Optional[str]:
        if not path:
            return None
        try:
            real = os.path.realpath(str(path))
            if not real.lower().endswith("_config.json"):
                return None
            if allowed_dir:
                root = os.path.realpath(str(allowed_dir))
                if os.path.commonpath([real, root]) != root:
                    return None
            if not os.path.isfile(real):
                return None
            return real
        except (OSError, ValueError):
            return None

    # ---------- 读取 ----------
    def _read(self, initial: bool = False) -> bool:
        with self._lock:
            return self._read_locked(initial)

    def _read_locked(self, initial: bool = False) -> bool:
        path = self.path
        if not path:
            return False
        try:
            stat = os.stat(path)
        except OSError as exc:
            self._warn("stat 失败: %s" % exc.__class__.__name__)
            return False
        if stat.st_size > self.max_bytes:
            self._warn("配置文件超过 %d 字节，拒绝加载" % self.max_bytes)
            return False
        try:
            with open(path, "rb") as fh:
                raw = fh.read(self.max_bytes + 1)
        except OSError as exc:
            self._warn("读取失败: %s" % exc.__class__.__name__)
            return False
        if len(raw) > self.max_bytes:
            self._warn("配置文件超过 %d 字节，拒绝加载" % self.max_bytes)
            return False
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            self._warn("JSON 解析失败: %s" % exc.__class__.__name__)
            return False
        if not isinstance(data, dict):
            self._warn("配置根节点不是对象，忽略")
            return False
        self._data = data
        self._mtime_ns = stat.st_mtime_ns
        self._size = stat.st_size
        self.last_error = ""
        if not initial:
            self.reload_count += 1
            logger.info("[amo_data] 配置文件已更新（%d 项，%d 字节）", len(data), stat.st_size)
        return True

    def _warn(self, message: str) -> None:
        self.last_error = message
        logger.warning("[amo_data] 配置热更新跳过：%s", message)

    # ---------- 对外 ----------
    def maybe_reload(self) -> bool:
        """文件变了才重读；返回 True 表示这次确实换了一份新配置。"""
        if not self.enabled:
            return False
        now = time.monotonic()
        with self._lock:
            if now < self._next_check:
                return False
            self._next_check = now + self.interval
            path = self.path
            if not path:
                return False
            try:
                stat = os.stat(path)
            except OSError as exc:
                self._warn("stat 失败: %s" % exc.__class__.__name__)
                return False
            if stat.st_mtime_ns == self._mtime_ns and stat.st_size == self._size:
                return False
            return self._read_locked()

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "path": os.path.basename(self.path or ""),
            "reloads": self.reload_count,
            "error": self.last_error,
        }
