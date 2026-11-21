# -*- coding: utf-8 -*-
"""services/chain.py —— 一条业务链的 [chain] trace（写入/召回每一步耗时）。

参考上游 chain.py 的思路，但只做插件内的一行日志，不落文件：
    [chain] recall trace=ab12cd34 step=vector ok n=12 (3ms)
配置 chain_trace=false 时整条静默（默认开）。

安全约束：
* detail 只放计数/ID，不拼用户原文，且强制去换行、截断——防止日志注入；
* 不读配置、不写文件、不联网，纯内存对象。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Optional

try:  # 包模式 / 顶层导入都能用
    from ..utils.text_process import logger
except ImportError:  # pragma: no cover - 测试脚本按文件加载时
    try:
        from astrbot.api import logger
    except ImportError:  # 纯离线脚本：trace 变空操作，不碰 logging 全局配置

        class _NullLogger:
            def info(self, *_args, **_kwargs):
                return None

            def warning(self, *_args, **_kwargs):
                return None

        logger = _NullLogger()

__all__ = ["Chain", "new_chain"]

MAX_DETAIL = 120


def _safe_detail(detail: Any) -> str:
    """日志安全：去掉换行/回车（防伪造新行），再截断。"""
    text = str(detail if detail is not None else "").replace("\r", " ").replace("\n", " ")
    return text.strip()[:MAX_DETAIL]


class Chain:
    """一次调用的 trace：每次 step 记「相对上一步的耗时」，done 记总耗时。"""

    def __init__(self, name: str, enabled: bool = True, trace_id: Optional[str] = None):
        self.name = str(name or "op")
        self.enabled = bool(enabled)
        self.trace_id = str(trace_id or uuid.uuid4().hex[:8])
        self._started = time.perf_counter()
        self._last = self._started

    def _emit(self, step: str, ok: bool, detail: str) -> None:
        if not self.enabled:
            return
        now = time.perf_counter()
        cost = max(0, int((now - self._last) * 1000))
        self._last = now
        tail = (" " + detail) if detail else ""
        if ok:
            logger.info("[chain] %s trace=%s step=%s ok%s (%dms)",
                        self.name, self.trace_id, step, tail, cost)
        else:
            logger.warning("[chain] %s trace=%s step=%s FAIL:%s (%dms)",
                           self.name, self.trace_id, step, detail or "unknown", cost)

    def step(self, step: str, detail: Any = "", ok: bool = True) -> None:
        self._emit(str(step or "step"), bool(ok), _safe_detail(detail))

    def fail(self, step: str, reason: Any) -> None:
        self._emit(str(step or "step"), False, _safe_detail(reason))

    def done(self, detail: Any = "") -> None:
        if not self.enabled:
            return
        total = max(0, int((time.perf_counter() - self._started) * 1000))
        tail = (" " + _safe_detail(detail)) if detail else ""
        logger.info("[chain] %s trace=%s done%s (%dms)", self.name, self.trace_id, tail, total)


def new_chain(name: str, enabled: bool = True) -> Chain:
    return Chain(name, enabled)
