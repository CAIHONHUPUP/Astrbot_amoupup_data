# -*- coding: utf-8 -*-
"""能记测试的公共夹具：新布局下的「最小可用栈」。

老测试（repo 的 test_*.py）都用 `store.DataStore` 一个类造库；现在拆成
models/memory_db（建表）+ services 各引擎，这里把常用组合装成一个 Stack，
迁移时只改引擎调用名，不再重复造表。
"""
from __future__ import annotations

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# CI（GitHub Actions）只装 requirements-dev.txt，没有 astrbot 运行库；
# 而新布局的 services/* 会 `from astrbot.api import logger`。这里补一个最小桩，
# 否则上传后 `pytest tests` 直接 ImportError（2026-11-24 实测）。
try:
    import astrbot.api  # noqa: F401
except Exception:  # pragma: no cover - 只在无 astrbot 的环境走
    import logging
    import types as _types

    _astrbot = _types.ModuleType("astrbot")
    _api = _types.ModuleType("astrbot.api")
    _api.logger = logging.getLogger("astrbot")
    _astrbot.api = _api
    sys.modules.setdefault("astrbot", _astrbot)
    sys.modules.setdefault("astrbot.api", _api)

from models.memory_db import MemoryDB  # noqa: E402
from services.backup_engine import BackupEngine  # noqa: E402
from services.cache_engine import CacheEngine  # noqa: E402
from services.compression_engine import CompressionEngine  # noqa: E402
from services.log_engine import LogEngine  # noqa: E402
from services.maintenance_engine import MaintenanceEngine  # noqa: E402
from services.message_engine import MessageEngine  # noqa: E402
from services.memory_engine import MemoryEngine  # noqa: E402
from services.person_engine import PersonEngine  # noqa: E402
from services.profile_engine import ProfileEngine  # noqa: E402
from services.retrieval_engine import RetrievalEngine  # noqa: E402


class Stack:
    """临时库 + 常用引擎；用完调用 close()。"""

    def __init__(self, tmpdir: str, name: str = "amo.db", settings=None):
        self.path = os.path.join(tmpdir, name)
        self.db = MemoryDB(self.path, dict(settings or {}))
        self.persons = PersonEngine(self.db, dict(settings or {}), None)
        self.profiles = ProfileEngine(self.db, dict(settings or {}), self.persons)
        self.messages = MessageEngine(self.db, dict(settings or {}), self.persons)
        self.memory = MemoryEngine(self.db, dict(settings or {}), self.persons)
        self.retrieval = RetrievalEngine(self.db, dict(settings or {}), self.persons, self.memory)
        self.compression = CompressionEngine(self.db, dict(settings or {}), self.memory)
        self.cache = CacheEngine(self.db)
        self.logs = LogEngine(self.db, dict(settings or {}), self.persons)
        self.backup = BackupEngine(self.db, tmpdir, dict(settings or {}))
        # 新布局里各引擎通过宿主互相取数据（person_detail 会读 plugin.profiles），
        # 夹具要把这根线接上，否则一调就 AttributeError。
        self.host = types.SimpleNamespace(
            profiles=self.profiles, persons=self.persons, messages=self.messages,
            memory=self.memory, retrieval=self.retrieval, compression=self.compression,
            cache=self.cache, logs=self.logs, backup=self.backup,
            settings=dict(settings or {}), db=self.db, context=None,
        )
        self.maintenance = MaintenanceEngine(self.host)
        self.host.maintenance = self.maintenance
        for engine in (self.persons, self.profiles, self.messages, self.memory,
                       self.retrieval, self.compression, self.logs):
            engine.plugin = self.host

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass

    @property
    def conn(self):
        """老测试大量直接 `store.conn.execute(...)`，这里给个等价出口。"""
        return self.db.conn

    @property
    def lock(self):
        return self.db.lock
