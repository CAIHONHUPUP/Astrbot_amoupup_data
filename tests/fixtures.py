# -*- coding: utf-8 -*-
"""能记测试共享夹具（Phase 13 P2）。

之前 8 个 Fake + 2 个 FakeDB 散在 6 个测试文件里，各写各的；这里统一收口，
测试文件只 import，不再各自定义。迁移只搬位置，断言一个没改。

内容：
* tp()/TempStore：临时目录与临时库（跑完删掉，遵守 conftest 的 .test-tmp）；
* frozen_now()：把「现在」钉死到 retrieval_engine / text_process / clock 全链路；
* FakeSettings：带 _cfg_int/_cfg_float/_cfg_bool 的设置桩；
* StubPluginDB / CacheDB / AmoStubDB / AmoStub：三种数据库桩；
* StubEngine / StubEvent / StubLogs / StubMessages：引擎/事件桩；
* StubRequest / NoJsonRequest / BoomHeader / WebContext：面板请求/上下文桩。
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone

_TESTS = os.path.dirname(os.path.abspath(__file__))
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

# Phase 18：FakeClock / temp_dir / temp_store / LogCapture / PII 断言统一从 support 复用。
from support import *  # noqa: E402,F401,F403
from support import SUPPORT_VERSION  # noqa: E402,F401

_CN = timezone(timedelta(hours=8))
#: 固定时钟：2026-11-16 21:30 +08:00（与召回 A/B 语料一致）
FIXED_NOW = int(datetime(2026, 11, 16, 21, 30, tzinfo=_CN).timestamp())
FIXED_DT = datetime(2026, 11, 16, 21, 30)


def tp(prefix: str = "fixture-") -> str:
    """在工作区临时目录里建一个子目录（优先 conftest 设的 .test-tmp）。"""
    base = os.environ.get("AMO_DATA_TEST_TMP") or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    return tempfile.mkdtemp(dir=base, prefix=prefix)


class TempStore:
    """临时 DataStore：with TempStore() as store: ... 退出时关闭并删目录。"""

    def __init__(self, prefix: str = "fixture-store-", settings=None):
        self.prefix = prefix
        self.settings = settings
        self.dir = ""
        self.path = ""
        self.store = None

    def __enter__(self):
        self.dir = tp(self.prefix)
        self.path = os.path.join(self.dir, "amo_data.db")
        from support.store_fixture import DataStore
        self.store = DataStore(self.path, settings=self.settings)
        return self.store

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.store is not None:
                self.store.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)
        return False


class FrozenClock:
    """冻结时钟的句柄：.now() 取固定时间戳，.exit() 恢复。"""

    def __init__(self, now_ts: int, now_dt: datetime):
        self.now_ts = int(now_ts)
        self.now_dt = now_dt
        self._patched = []
        self._saved_clock = None

    def __enter__(self):
        import utils.clock as clock_mod
        import utils.text_process as text_mod
        original = text_mod._now
        self._saved_clock = clock_mod.now
        # 凡是「from ... import _now」按值拿过去的模块，一并换成假时钟
        for name, module in list(sys.modules.items()):
            if module is None or not name:
                continue
            try:
                current = getattr(module, "_now", None)
            except Exception:
                continue
            if current is original:
                self._patched.append((module, current))
                module._now = lambda: self.now_ts
        clock_mod.now = lambda explicit="": self.now_dt
        return self

    def now(self) -> int:
        return self.now_ts

    def exit(self) -> None:
        for module, original in self._patched:
            module._now = original
        self._patched = []
        if self._saved_clock is not None:
            import utils.clock as clock_mod
            clock_mod.now = self._saved_clock
            self._saved_clock = None

    def __exit__(self, exc_type, exc, tb):
        self.exit()
        return False


def frozen_now(now_ts: int = FIXED_NOW, now_dt: datetime = FIXED_DT) -> FrozenClock:
    """把「现在」钉死（可 with，也可手工 .exit()）。"""
    return FrozenClock(now_ts, now_dt)


class FakeSettings(dict):
    """设置桩：dict + 插件用的三个取值助手。"""

    def __init__(self, values=None):
        super().__init__(values or {})

    def _cfg_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return int(default or 0)

    def _cfg_float(self, key, default=0.0) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return float(default or 0.0)

    def _cfg_bool(self, key, default=False) -> bool:
        value = self.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(value)


class StubPluginDB:
    """对象式数据库桩：不建连接，只记录 close/bot_ids（入口胶水用）。"""

    def __init__(self):
        self.conn = object()
        self.lock = object()
        self._conn = object()
        self.closed = False
        self.path = "x.db"

    def close(self):
        self.closed = True

    def bot_ids(self):
        return ["bot-a"]


class CacheDB:
    """sqlite 内存库桩：只带 cache 表（缓存引擎用）。"""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE cache(bot_id TEXT, key TEXT, value TEXT, "
            "updated_at INTEGER, expires_at INTEGER, PRIMARY KEY(bot_id, key))"
        )
        self.lock = threading.Lock()


class StubEngine:
    """最小引擎桩：记一次 ping。"""

    def __init__(self):
        self.flag = 0

    def ping(self):
        return "pong"


class StubEvent:
    """事件桩：message_str + plain_result。"""

    def __init__(self, text=""):
        self.message_str = text

    def plain_result(self, text):
        return text


class StubLogs:
    """日志引擎桩：记录 purge_logs 调用。"""

    def __init__(self):
        self.calls = []

    def purge_logs(self, bot_id, days):
        self.calls.append((bot_id, days))
        return 2


class StubMessages:
    """消息引擎桩：compress_messages 返回固定结果。"""

    def compress_messages(self, bot_id, after, days):
        return {"compressed": 1, "after": after, "days": days}


class StubRequest:
    """最小 quart/astrbot 请求桩。"""

    def __init__(self, payload=None, method="POST", headers=None,
                 json_error=False, get_json_error=False):
        self.payload = {} if payload is None else payload
        self.method = method
        self.headers = {} if headers is None else headers
        self.json_error = json_error
        self.get_json_error = get_json_error

    async def json(self, default=None):
        if self.json_error:
            raise RuntimeError("没有 json")
        return self.payload

    async def get_json(self, silent=True):
        if self.get_json_error:
            raise RuntimeError("没有 get_json")
        return self.payload


class NoJsonRequest:
    """只有 get_json、没有 json 的旧版请求对象。"""

    def __init__(self, payload):
        self.payload = payload

    async def get_json(self, silent=True):
        return self.payload


class BoomHeader:
    """headers.get 直接抛错：守卫要能放行而不是 500。"""

    def get(self, key):
        raise RuntimeError("header 挂了")


class WebContext:
    """面板上下文桩：记录注册的路由，get_config 返回空。"""

    def __init__(self, registry=None, raise_on_register=False):
        self.registered = []
        self.registered_web_apis = list(registry or [])
        self.raise_on_register = raise_on_register

    def register_web_api(self, route, handler, methods, desc):
        if self.raise_on_register:
            raise RuntimeError("注册失败")
        self.registered.append((route, methods, desc))
        self.registered_web_apis.append((route, methods, handler))

    def get_config(self, umo=None):
        return {}


class AmoStubDB:
    """陪伴插件本地库桩：只记录 delete_profile。"""

    def __init__(self):
        self.deleted = []

    def delete_profile(self, uid):
        self.deleted.append(uid)


class AmoStub:
    """陪伴插件桩：挂一个 AmoStubDB。"""

    def __init__(self):
        self.db = AmoStubDB()


class AmoContext:
    """上下文桩：get_registered_star 返回陪伴插件。"""

    def __init__(self, amo):
        self.amo = amo

    def get_registered_star(self, name):
        import types
        return types.SimpleNamespace(star_cls=self.amo)
