# -*- coding: utf-8 -*-
"""能记的本地回归入口（线上目录原来没有 tests，改代码全靠手测）。

只覆盖纯函数/常量，不建库、不连网：
- 时间词解析（「昨晚」必须能解析出时间窗）；
- 召回的时间收窄规则；
- 已退役的 finance_* 不能残留在 kind 白名单里。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone  # noqa: E402

from utils import time_query as tq  # noqa: E402
from services import lifecycle, memory_decay, retrieval_engine  # noqa: E402
from core import memory_tooling  # noqa: E402

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 24, 16, 7, 0, tzinfo=TZ)


def _local(ts):
    return datetime.fromtimestamp(int(ts), TZ)


def test_yesterday_evening_parses():
    span = tq.parse_time_span("我昨晚最后一句话是啥", now=NOW.replace(tzinfo=None))
    assert span, "「昨晚」必须能解析出时间窗"
    assert span["granularity"] == "hour"
    # 和旧路径的 _PERIODS 对齐：晚上=18:00-24:00（面板搜索与聊天回看同一口径）
    assert _local(span["start"]) == datetime(2026, 11, 23, 18, 0, tzinfo=TZ)
    assert _local(span["end"]) == datetime(2026, 11, 24, 0, 0, tzinfo=TZ)


def test_engine_window_matches_parser():
    """面板搜索（MessageEngine）和聊天回看（parse_time_span）必须同一口径。"""
    from services.message_engine import MessageEngine

    span = tq.parse_time_span("昨晚我们聊了什么", now=NOW.replace(tzinfo=None))
    start, end = MessageEngine._time_window("昨晚我们聊了什么")
    assert (start, end) == (int(span["start"]), int(span["end"]))


def test_last_night_variants_parse():
    for text in ("昨晚睡前我跟你说什么了", "我们昨晚聊了什么", "老婆你还记得昨晚吗"):
        span = tq.parse_time_span(text, now=NOW.replace(tzinfo=None))
        assert span, text


def test_time_recall_window_only_for_past_words():
    span = tq.parse_time_span("昨晚我们聊了什么", now=NOW.replace(tzinfo=None))
    win = retrieval_engine._recall_time_window("昨晚我们聊了什么", span)
    assert win is not None
    lo, hi = win
    assert lo <= int(span["start"]) and hi >= int(span["end"])


def test_time_recall_window_skips_neutral_queries():
    span = tq.parse_time_span("昨天我们聊了什么", now=NOW.replace(tzinfo=None))
    assert retrieval_engine._recall_time_window("在吗", span) is None
    assert retrieval_engine._recall_time_window("昨天我们聊了什么", None) is None


def test_finance_kinds_are_gone():
    assert not any(k.startswith("finance") for k in memory_decay.NOISE_KINDS)
    assert not any(k.startswith("finance") for k in memory_tooling.NOISY_KINDS)
    assert not any(k.startswith("finance") for k in lifecycle.KIND_DORMANT_DAYS)
    assert not any(k.startswith("finance") for k in lifecycle.KIND_EXPIRE_DAYS)
    assert not any(k.startswith("finance") for k in memory_decay.KIND_HALF_LIFE_DAYS)
