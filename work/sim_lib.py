# -*- coding: utf-8 -*-
"""work 脚本共用仿真库：astrbot 桩 / 假库 / 假插件 / 假时钟。

tests/test_test_infra.py 要求 work/ 下的脚本共用这一份，不各写各的 Fake。
这里的实现只用于离线评估与仿真，**不碰真实库**。
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import threading
from typing import Any, Dict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")


def install_stub():
    """把 ROOT / tests 放进 sys.path 并安装 astrbot 桩（幂等）。"""
    for path in (ROOT, TESTS):
        if path not in sys.path:
            sys.path.insert(0, path)
    import astrbot_stub
    astrbot_stub.install()
    return astrbot_stub


install_stub()


class FakeDB:
    """内存 sqlite + 锁：只提供引擎需要的 .conn / .lock / .path / .setting()。"""

    def __init__(self, path=":memory:", settings=None):
        self.path = str(path)
        self.settings = dict(settings or {})
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.closed = False

    def setting(self, key, default=None):
        return self.settings.get(key, default)

    def bot_ids(self):
        return []

    def close(self):
        if not self.closed:
            self.conn.close()
            self.closed = True


class FakePlugin:
    """settings + 记忆引擎三件套；基于一次性临时库，用完 close()。"""

    def __init__(self, settings=None, db_path=""):
        from utils.text_process import DEFAULT_SETTINGS
        from models.memory_db import MemoryDB
        from services.memory_engine import MemoryEngine
        from services.person_engine import PersonEngine
        from services.retrieval_engine import RetrievalEngine

        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update({k: v for k, v in (settings or {}).items() if v is not None})
        self.dir = tempfile.mkdtemp(prefix="amo-sim-")
        self.db_path = db_path or os.path.join(self.dir, "amo_data.db")
        self.db = MemoryDB(self.db_path, settings=self.settings)
        self.persons = PersonEngine(self.db)
        self.memory = MemoryEngine(self.db, self.settings, persons=self.persons)
        self.retrieval = RetrievalEngine(self.db, self.settings, persons=self.persons,
                                         memory=self.memory)
        for engine in (self.persons, self.memory, self.retrieval):
            engine.plugin = self
        self.persons.bootstrap()

    @property
    def conn(self):
        return self.db.conn

    def remember(self, bot_id, uid, content, **kwargs):
        return self.memory.remember(bot_id, uid, content, **kwargs)

    def recall(self, bot_id, uid, query="", limit=6, **kwargs):
        return self.retrieval.recall(bot_id, uid, query, limit=limit, **kwargs)

    def close(self):
        import shutil
        try:
            self.db.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)


#: 仿真用的固定「现在」：2026-11-17 21:30 +08:00（「昨天」= 11-16，对得上日总结）
SIM_NOW = int(dt.datetime(2026, 11, 17, 21, 30, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp())
SIM_BOT = "sim-bot"
SIM_UID = "sim-user"
#: 固定语料：偏好 / 近重复 / 罚款流水 / 日总结 / 无关
SIM_ROWS = (
    ("主人喜欢吃螺蛳粉，尤其是加辣的", {"kind": "fact", "importance": 0.8}),
    ("螺蛳粉真好吃，昨天又去吃了", {"kind": "fact", "importance": 0.5}),
    ("主人养了一只叫团子的橘猫", {"kind": "fact", "importance": 0.6}),
    ("2026-11-17 00:00 支出 95.93 元（迟到罚款），余额 6,235.02 元",
     {"kind": "finance_long_expense", "importance": 0.75}),
    ("【2026-11-16】他当天在家修 bug，中午吃了猪肉粽，晚上陪我聊天。",
     {"kind": "daily_digest", "importance": 0.75}),
    ("主人对芒果过敏，不能吃", {"kind": "fact", "importance": 0.9}),
)


def seed(plugin, bot_id=SIM_BOT, uid=SIM_UID, rows=None):
    """往假插件里灌固定语料；返回写入条数。"""
    written = 0
    for content, kwargs in (rows or SIM_ROWS):
        plugin.remember(bot_id, uid, content, **kwargs)
        written += 1
    return written


#: 假时钟状态：ts / 被替换的模块列表 / 原始 utils.clock.now
_CLOCK: Dict[str, Any] = {"ts": None, "patched": [], "clock_now": None}


def set_clock(ts, now_dt=None):
    """把「现在」钉到 ts（同时替换所有按值 import 了 _now 的模块）。"""
    import utils.text_process as text_mod
    import utils.clock as clock_mod
    ts = int(ts)
    if _CLOCK["ts"] is not None:
        restore_clock()
    original = text_mod._now
    for name, module in list(sys.modules.items()):
        if module is None or not name:
            continue
        try:
            current = getattr(module, "_now", None)
        except Exception:
            continue
        if current is original:
            _CLOCK["patched"].append((module, current))
            module._now = lambda: ts
    _CLOCK["clock_now"] = clock_mod.now
    moment = now_dt or dt.datetime.fromtimestamp(ts)
    clock_mod.now = lambda explicit="": moment
    _CLOCK["ts"] = ts
    return ts


def restore_clock():
    """恢复真时钟。"""
    import utils.clock as clock_mod
    for module, original in _CLOCK["patched"]:
        module._now = original
    _CLOCK["patched"] = []
    if _CLOCK["clock_now"] is not None:
        clock_mod.now = _CLOCK["clock_now"]
    _CLOCK["clock_now"] = None
    _CLOCK["ts"] = None
