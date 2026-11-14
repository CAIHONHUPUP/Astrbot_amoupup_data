# -*- coding: utf-8 -*-
"""我能永远记住你（Astrbot_amoupup_data）——极简入口层（v4 架构）。

这一层只做装配与注册，不写 SQL、不做打分、不判去重：

    main.py
      |- 配置        -> utils/config.PluginSettings
      |- 建库/迁移    -> models/memory_db.MemoryDB
      |- 业务        -> services/*.py
      |- 对外门面     -> core/api.py      (self.mem，守护/陪伴只认这个)
      |- 面板路由     -> core/web_api.py  (WebAPIMixin)
      '- 维护循环     -> services/maintenance_engine.py

兼容红线（改名/改参数/改默认值都算破坏）：
    * self.mem 的 39 个 async 方法签名（见 core/api.py 顶部契约清单）；
    * plugin_data/Astrbot_amoupup_data/amo_data.db 的路径、表名、列名、索引名；
    * /Astrbot_amoupup_data/* 面板路由、methods 与返回结构。

导入约定沿用旧代码：包模式优先，测试按顶层 import main 时回退到同级导入。
"""

from __future__ import annotations

import os
from typing import Optional

from astrbot.api import logger
from astrbot.api.star import Context, Star

try:  # 命令装饰器；离线单测没有 astrbot.api.event 时退化成空装饰器
    from astrbot.api.event import filter as _filter
except Exception:  # pragma: no cover - 离线环境
    _filter = None


def _command(name):
    finder = getattr(_filter, "command", None) if _filter is not None else None
    if callable(finder):
        return finder(name)

    def _deco(func):
        return func

    return _deco

try:  # AstrBot 正常加载（包模式）
    from .core.api import MemoryAPI
    from .core.web_api import WebAPIMixin, web_request
    from .models.memory_db import MemoryDB
    from .services.backup_engine import BackupEngine
    from .services.cache_engine import CacheEngine
    from .services.compression_engine import CompressionEngine
    from .services.log_engine import LogEngine
    from .services.maintenance_engine import MaintenanceEngine
    from .services.memory_engine import MemoryEngine
    from .services.message_engine import MessageEngine
    from .services.person_engine import PersonEngine
    from .services.profile_engine import ProfileEngine
    from .services.retrieval_engine import RetrievalEngine
    from .utils import clock
    from .utils.config import PluginSettings
except ImportError:  # 测试/单文件按顶层 import main 时
    from core.api import MemoryAPI
    from core.web_api import WebAPIMixin, web_request
    from models.memory_db import MemoryDB
    from services.backup_engine import BackupEngine
    from services.cache_engine import CacheEngine
    from services.compression_engine import CompressionEngine
    from services.log_engine import LogEngine
    from services.maintenance_engine import MaintenanceEngine
    from services.memory_engine import MemoryEngine
    from services.message_engine import MessageEngine
    from services.person_engine import PersonEngine
    from services.profile_engine import ProfileEngine
    from services.retrieval_engine import RetrievalEngine
    from utils import clock
    from utils.config import PluginSettings

_clock = clock  # 兼容老测试/脚本：main._clock

PLUGIN_NAME = "Astrbot_amoupup_data"
PLUGIN_VERSION = "3.2.1"  # 与 metadata.yaml 保持一致；发版时统一 bump
DEFAULT_BOT_ID = "default"



class _PluginStoreView:
    """迁移期兼容：面板/测试里的 plugin.store 仍按 3.0 的同步 API 用。"""

    def __init__(self, plugin):
        self._p = plugin

    @property
    def conn(self):
        return self._p.db.conn

    @property
    def lock(self):
        return self._p.db.lock

    def setting(self, key, default=None):
        return self._p.settings.get(key, default)

    @property
    def _conn(self):
        return self._p.db._conn

    @property
    def _loop_task(self):
        return getattr(self._p, "_loop_task", None)

    def close(self):
        return self._p.db.close()

    def __setattr__(self, name, value):
        if name != "_p" and self.__dict__.get("_p") is not None:
            for attr in ("memory", "retrieval", "compression", "profiles", "persons",
                         "messages", "logs", "cache", "backup", "maintenance"):
                engine = getattr(self._p, attr, None)
                if engine is not None and hasattr(engine, name):
                    setattr(engine, name, value)
                    return
        object.__setattr__(self, name, value)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        for attr in ("memory", "retrieval", "compression", "profiles", "persons",
                     "messages", "logs", "cache", "backup", "maintenance"):
            engine = getattr(self._p, attr, None)
            if engine is not None:
                value = getattr(engine, name, None)
                if callable(value):
                    return value
        value = getattr(self._p.db, name, None)
        if callable(value):
            return value
        raise AttributeError(name)

class AmoDataPlugin(WebAPIMixin, Star):
    @_command("mem_stats")
    async def mem_stats_cmd(self, event):
        """只读：查看记忆库规模（/mem_stats）。"""
        try:
            data = await self.mem.stats(self._bot_id({}, write=False)) or {}
            text = ("记忆 %s 条（活跃 %s）· 画像 %s · 人物 %s · 消息 %s · 关系 %s"
                    % (data.get("memories", 0), data.get("memories_active", 0),
                       data.get("profiles", 0), data.get("persons", 0),
                       data.get("messages", 0), data.get("relations", 0)))
        except Exception as exc:
            logger.warning("[amo_data] /mem_stats 失败: %s" % exc)
            text = "记忆库统计失败：%s" % exc
        yield event.plain_result(text)

    @_command("遗忘")
    async def forget_cmd(self, event):
        """管理员 /遗忘 <关键词>：物理删除包含关键词的记忆。"""
        raw = str(getattr(event, "message_str", "") or "").replace("／", "/").strip()
        parts = [part for part in raw.split() if part and not part.startswith("@")]
        keyword = " ".join(parts[1:]).strip()
        if not keyword:
            yield event.plain_result("用法：/遗忘 <关键词>")
            return
        try:
            count = self.memory.forget(self._bot_id({}, write=False), keyword)
            yield event.plain_result("已从记忆中抹除关于 %s 的 %d 条记录。" % (keyword, count))
        except Exception as exc:
            logger.warning("[amo_data] /遗忘 失败: %s" % exc)
            yield event.plain_result("遗忘失败：%s" % exc)

    """插件入口：装配 models / services / core，注册面板接口与维护循环。"""

    def __init__(self, context: Context, config: Optional[dict] = None):
        super().__init__(context)
        self.config: dict = config or {}
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.data_dir = self._resolve_data_dir()
        self.timezone_name = self._setup_timezone()

        # 配置 -> 强类型设置（原 _cfg_* / _store_settings / chat_days 等）
        self.settings = PluginSettings(self.config)

        # ---- models：连接、建表、迁移、WAL、锁、备份文件 ----
        self.db = MemoryDB(os.path.join(self.data_dir, "amo_data.db"))

        # ---- services：先有人物/画像，记忆引擎要按 pid 归一身份 ----
        self.persons = PersonEngine(self.db)
        self.profiles = ProfileEngine(self.db, persons=self.persons)
        self.memory = MemoryEngine(self.db, self.settings, persons=self.persons)
        self.retrieval = RetrievalEngine(
            self.db, self.settings, persons=self.persons, memory=self.memory
        )
        self.compression = CompressionEngine(self.db, self.settings, memory=self.memory)
        self.messages = MessageEngine(self.db, self.settings)
        self.logs = LogEngine(self.db)
        self.cache = CacheEngine(self.db)
        self.backup = BackupEngine(self.db, self.data_dir, self.settings)
        self.maintenance = MaintenanceEngine(self)

        # 引擎之间通过 plugin 互相调用（人物/聊天/备份/维护）
        for engine in (self.persons, self.profiles, self.memory, self.retrieval,
                       self.compression, self.messages, self.logs, self.cache, self.backup):
            engine.plugin = self

        # ---- core：跨插件门面 + 面板路由 ----
        self.mem = MemoryAPI(self)
        self.db_path = self.db.path
        self.store = _PluginStoreView(self)
        self.persons.bootstrap()
        self._safe_register_web_apis()

        # ---- 维护循环：每小时压缩/清理/每周整理/每日备份/向量补全 ----
        self._loop_task = None  # 兼容老代码/测试：任务由 MaintenanceEngine 持有
        self.maintenance.start()
        self._loop_task = self.maintenance._task

        logger.info("[amo_data] 我能永远记住你 %s 已加载（%s）", PLUGIN_VERSION, self.db.path)

    def _resolve_data_dir(self) -> str:
        """AstrBot 给了 data_dir 就用它，否则退回 plugin_data/<插件名>。"""
        data_dir = str(getattr(self, "data_dir", "") or "").strip()
        if not data_dir:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path
                data_dir = os.path.join(str(get_astrbot_data_path()), "plugin_data", PLUGIN_NAME)
            except Exception:
                data_dir = os.path.join(self.plugin_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return data_dir

    def _setup_timezone(self) -> str:
        try:
            name = clock.configure(str(self.config.get("timezone") or "Asia/Shanghai"))
            logger.info("[amo_data] 时区: %s", name)
            return name
        except Exception:
            return ""

    def _bot_id(self, payload: Optional[dict] = None, write: bool = False) -> str:
        """读：留空表示所有 bot；写：留空时自动落到唯一的 bot。"""
        data = payload or {}
        value = str(data.get("bot_id") or "").strip()
        if value:
            return value
        conf = str(self.config.get("bot_id") or "").strip()
        if conf and conf != DEFAULT_BOT_ID:
            return conf
        if not write:
            return ""
        try:
            ids = self.db.bot_ids()
        except Exception:
            ids = []
        if len(ids) == 1:
            return ids[0]
        return conf or DEFAULT_BOT_ID


    # ---- 迁移期兼容（老面板代码/测试仍会调这些） ----
    def _cfg_int(self, key: str, default: int) -> int:
        return self.settings._cfg_int(key, default)

    def _cfg_float(self, key: str, default: float) -> float:
        return self.settings._cfg_float(key, default)

    def _cfg_bool(self, key: str, default: bool) -> bool:
        return self.settings._cfg_bool(key, default)

    def _store_settings(self) -> dict:
        return dict(self.settings._values)

    @property
    def chat_days(self) -> int:
        return self.settings.chat_days

    @property
    def compress_after(self) -> int:
        return self.settings.compress_after

    @property
    def log_days(self) -> int:
        return self.settings.log_days

    async def _maintenance_loop(self) -> None:
        await self.maintenance._loop()

    async def _maybe_daily_backup(self, bot_id: str) -> str:
        return await self.maintenance._maybe_daily_backup(bot_id)

    async def _maybe_weekly_cleanup(self, bot_id: str) -> dict:
        return await self.maintenance._maybe_weekly_cleanup(bot_id)

    async def terminate(self) -> None:
        try:
            if getattr(self, "_loop_task", None) is not None:
                self._loop_task.cancel()
        except Exception:
            pass
        try:
            await self.maintenance.stop()
        except Exception:
            pass
        try:
            # 向量补全后台线程收尾，否则每次热重载泄漏一个线程
            self.memory.close()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        logger.info("[amo_data] 我能永远记住你已卸载")
