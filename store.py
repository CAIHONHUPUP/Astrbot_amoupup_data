# -*- coding: utf-8 -*-
"""store.py —— 兼容壳（弃用）：DataStore = 新引擎的聚合，只做转发。

老测试/脚本仍能 from store import DataStore；迁移期需要同步 API 的地方不用改。
新代码请直接用 services/*。
"""

from __future__ import annotations

from models.memory_db import MemoryDB
from services.person_engine import PersonEngine
from services.profile_engine import ProfileEngine
from services.memory_engine import MemoryEngine
from services.retrieval_engine import RetrievalEngine
from services.compression_engine import CompressionEngine
from services.message_engine import MessageEngine
from services.log_engine import LogEngine
from services.cache_engine import CacheEngine
from services.backup_engine import BackupEngine
from services.maintenance_engine import MaintenanceEngine
from utils.text_process import (
    DEFAULT_SETTINGS, RECALL_HALF_LIFE_DAYS, RECALL_IMPORTANCE_DECAY, RECALL_POOL,
    RECALL_W_FRESH, RECALL_W_IMPORTANCE, RECALL_W_REL, STOP_BIGRAMS, STOP_CHARS,
    PLATFORM_ALIASES, PLATFORM_LABELS, KNOWN_PLATFORMS,
    _bm25_scores, _bot_filter, _clean, _cosine, _from_vec, _freshness,
    _informative_terms, _jaccard, _name_key, _norm_key, _now, _parse_time_span,
    _query_chars, _recall_tokens, _slot_facts, _to_vec, _unique_labels,
    logger, normalize_platform, platform_label, platform_of_umo,
)

__all__ = [
    "DataStore", "DEFAULT_SETTINGS", "_slot_facts", "_clean", "_norm_key", "_now",
    "_bot_filter", "_parse_time_span", "platform_label", "normalize_platform",
    "platform_of_umo", "_unique_labels", "_recall_tokens", "_bm25_scores",
    "_freshness", "_jaccard", "_cosine", "_informative_terms", "_query_chars",
    "_to_vec", "_from_vec", "_name_key", "RECALL_POOL", "RECALL_HALF_LIFE_DAYS",
    "RECALL_W_REL", "RECALL_W_IMPORTANCE", "RECALL_W_FRESH", "RECALL_IMPORTANCE_DECAY",
    "STOP_CHARS", "STOP_BIGRAMS", "logger",
]

_ENGINE_ATTRS = ("memory", "retrieval", "compression", "profiles", "persons",
                 "messages", "logs", "cache", "backup", "maintenance")


class DataStore:
    """把各引擎拼成 3.0 的同步 API（迁移期兼容，后续删除）。"""

    def __init__(self, db_path: str, settings=None):
        self.path = str(db_path)
        self.settings = dict(DEFAULT_SETTINGS)
        if settings:
            self.settings.update({k: v for k, v in settings.items() if v is not None})
        self.db = MemoryDB(self.path, settings=self.settings)
        s = self.settings
        self.persons = PersonEngine(self.db, s)
        self.profiles = ProfileEngine(self.db, persons=self.persons)
        self.memory = MemoryEngine(self.db, s, persons=self.persons)
        self.retrieval = RetrievalEngine(self.db, s, persons=self.persons, memory=self.memory)
        self.compression = CompressionEngine(self.db, s, memory=self.memory)
        self.messages = MessageEngine(self.db, s)
        self.logs = LogEngine(self.db)
        self.cache = CacheEngine(self.db)
        self.backup = BackupEngine(self.db, self._data_dir(), s)
        self.maintenance = MaintenanceEngine(self)
        for engine in (self.persons, self.profiles, self.memory, self.retrieval,
                       self.compression, self.messages, self.logs, self.cache, self.backup):
            engine.plugin = self
        self._fts_tri = self.db.fts_trigram
        self.persons.bootstrap()

    def _data_dir(self):
        import os
        return os.path.dirname(self.path)

    # ---- 老接口 ----
    @property
    def conn(self):
        return self.db.conn

    @property
    def lock(self):
        return self.db.lock

    def setting(self, key: str, default=None):
        return self.settings.get(key, DEFAULT_SETTINGS.get(key, default))

    def close(self) -> None:
        try:
            self.memory.close()
        finally:
            self.db.close()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        for engine_name in _ENGINE_ATTRS:
            engine = self.__dict__.get(engine_name)
            if engine is None:
                continue
            attr = getattr(engine, name, None)
            if callable(attr):
                return attr
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name == "setting" and self.__dict__.get("memory") is not None:
            object.__setattr__(self, name, value)
            for engine_name in _ENGINE_ATTRS:
                engine = self.__dict__.get(engine_name)
                if engine is not None:
                    engine.setting = value
            return
        if not name.startswith("_") or name.startswith("__"):
            object.__setattr__(self, name, value)
            return
        # 测试会替换引擎的私有方法（store._embed = fake），转发到真正持有它的引擎
        for engine_name in _ENGINE_ATTRS:
            engine = self.__dict__.get(engine_name)
            if engine is not None and hasattr(engine, name):
                setattr(engine, name, value)
                return
        object.__setattr__(self, name, value)


# 老测试直接按类调用这几个时间解析方法（3.0 是 DataStore 的 classmethod/staticmethod）
DataStore._time_window = classmethod(lambda cls, query: MessageEngine._time_window(query))
DataStore._cn_int = staticmethod(lambda value: MessageEngine._cn_int(value))
DataStore._day_window = staticmethod(lambda day, text: MessageEngine._day_window(day, text))
DataStore._time_label = staticmethod(lambda query: MessageEngine._time_label(query))
DataStore._epoch = staticmethod(lambda value: MessageEngine._epoch(value))
