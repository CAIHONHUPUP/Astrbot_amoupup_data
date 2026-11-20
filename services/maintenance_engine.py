# -*- coding: utf-8 -*-
"""services/maintenance_engine.py —— 每小时的保留策略调度 + 面板统计。

循环内容与 3.0 一致：压缩老聊天、清过期日志、每周记忆整理、每日备份、向量补全。
调度改由 services/jobs.py 的 JobManager 托管（Phase 16，与陪伴/守护同一契约）：
首跑 60 秒、之后 3600 秒；单飞、单次超时、失败退避、stop 统一取消；
report() 可看到 last_run/fail_count/next_run 等快照。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

try:
    from ..utils.clock import now as _clock_now
except ImportError:  # 顶层导入（测试/脚本）
    from utils.clock import now as _clock_now

try:
    from ..utils.text_process import _bot_filter, logger
except ImportError:  # 顶层导入（测试/脚本）
    from utils.text_process import _bot_filter, logger

try:
    from .jobs import JobManager
    from ..services import entity_graph, lifecycle
except ImportError:  # 顶层导入（测试/脚本）
    from services.jobs import JobManager
    from services import entity_graph, lifecycle


class MaintenanceEngine:
    def __init__(self, plugin):
        self.plugin = plugin
        self.db = plugin.db
        self.settings = plugin.settings
        self._task = None
        self._running = False
        # Phase 16：首跑 60s、之后 3600s；单飞/超时/退避/取消都交给 JobManager
        self.jobs = JobManager(logger=logger, tag="amo_data", startup_delay=60.0)

    # ---------- 调度 ----------
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # 没有事件循环（脚本/单测）就不起后台任务
        self.jobs.register("maintenance", self.run_once, 3600,
                           immediate=True, timeout=1800)
        self.jobs.start()
        self._task = self.jobs.task("maintenance")
        self._running = self._task is not None

    async def stop(self) -> None:
        self._running = False
        try:
            await self.jobs.stop()
        except Exception as exc:
            logger.debug("[amo_data] 维护循环停止失败: %r", exc)
        self._task = None

    async def _loop(self) -> None:
        """兼容入口：注册后等待停止；实际调度由 JobManager 负责。

        首跑仍是 60 秒、之后 3600 秒（以前是先 sleep 3600 再跑：只要热重载间隔
        小于 1 小时，日备份/周整理就永远不执行）。
        """
        self.jobs.register("maintenance", self.run_once, 3600,
                           immediate=True, timeout=1800)
        self.jobs.start()
        self._task = self.jobs.task("maintenance")
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            return

    async def run_once(self) -> Dict[str, Any]:
        bot_id = self.plugin._bot_id({}, write=True)  # 空 bot_id 会让清理/压缩变成空转
        result: Dict[str, Any] = {}
        try:
            # retention 是同步函数（sqlite + 压缩），放线程池跑，别占事件循环
            result["retention"] = await asyncio.to_thread(
                self.retention, bot_id, self.plugin.settings.chat_days,
                self.plugin.settings.compress_after, self.plugin.settings.log_days,
            )
        except Exception as exc:
            result["chat_error"] = str(exc)
        try:
            await self._maybe_weekly_cleanup(bot_id)
        except Exception as exc:
            logger.debug("[amo_data] 每周记忆整理失败: %s" % exc)
        try:
            await self._maybe_daily_backup(bot_id)
        except Exception as exc:
            logger.debug("[amo_data] 每日备份失败: %s" % exc)
        try:
            if self.settings.get("embedding_enabled"):
                self.plugin.memory.backfill_embeddings(
                    limit=int(self.settings.get("embedding_backfill_limit", 200) or 200)
                )
        except Exception as exc:
            logger.debug("[amo_data] 向量补全失败: %s" % exc)
        try:
            if self.settings.get("lifecycle_enabled", True):
                result["lifecycle"] = await asyncio.to_thread(self._maybe_lifecycle, bot_id)
        except Exception as exc:
            result["lifecycle_error"] = str(exc)
        try:
            if self.settings.get("semantic_consolidation_enabled", True):
                result["semantic"] = await self._maybe_semantic(bot_id)
        except Exception as exc:
            result["semantic_error"] = str(exc)
        return result

    async def _maybe_semantic(self, bot_id: str) -> Dict[str, Any]:
        """每天一次：跨天同主题语义巩固（最多 3 组；拿不到 provider 就跳过）。"""
        import time
        now = int(time.time())
        key = "semantic_day_last"
        last = 0  # 缓存读不到就按「从没跑过」处理，避免下面 last 未定义
        try:
            last = int(self.plugin.cache.cache_get(bot_id, key) or 0)
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)
        if not lifecycle.should_run(last, now):
            return {"skipped": True, "last": last}
        out = await self.plugin.mem.consolidate_semantic(bot_id)
        try:
            self.plugin.cache.cache_set(bot_id, key, str(now), ttl=40 * 86400)
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)
        logger.info("[amo_data] 语义巩固: %s" % out)
        return out

    def _maybe_lifecycle(self, bot_id: str) -> Dict[str, Any]:
        """每天一次：按 kind 的 TTL 休眠/过期，顺手给老记忆补实体图谱。"""
        import time
        now = int(time.time())
        key = "lifecycle_last"
        last = 0
        try:
            last = int(self.plugin.cache.cache_get(bot_id, key) or 0)
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)
        # 错过补偿：不是「今天跑过就跳」，而是「距上次 ≥20 小时才跳」——
        # 进程停两天，下次起来照常补跑一次（plan 按年龄算，天然幂等）
        if not lifecycle.should_run(last, now):
            return {"skipped": True, "last": last}
        out = self.plugin.memory.run_lifecycle(bot_id, apply=True)
        try:
            if self.settings.get("recall_entity_graph", True):
                out["graph_backfill"] = entity_graph.backfill(
                    self.plugin.memory.dao, bot_id, 2000)
        except Exception as exc:
            out["graph_error"] = str(exc)
        try:
            self.plugin.cache.cache_set(bot_id, key, str(now), ttl=40 * 86400)
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)
        logger.info("[amo_data] 生命周期: %s" % out)
        return out

    # ---------- 保留策略 ----------
    def retention(self, bot_id: str, chat_days: int = 14, compress_after: int = 3,
                  log_days: int = 7) -> Dict[str, Any]:
        """聊天先压缩再删除，日志到期删除。"""
        result: Dict[str, Any] = {}
        try:
            result["chat"] = self.plugin.messages.compress_messages(
                bot_id, int(compress_after), int(chat_days))
        except Exception as exc:
            result["chat_error"] = str(exc)
        try:
            result["logs_purged"] = self.plugin.logs.purge_logs(bot_id, int(log_days))
        except Exception as exc:
            result["log_error"] = str(exc)
        return result

    # ---------- 周期任务 ----------
    async def _maybe_weekly_cleanup(self, bot_id: str) -> Dict[str, Any]:
        """每周只跑一次：合并同天摘要、近重复去重、归档低价值老记忆。"""
        week = _clock_now().strftime("%G-W%V")
        key = "cleanup_week:" + week
        try:
            if self.plugin.cache.cache_get(bot_id, key):
                return {"skipped": True}
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)
        result: Dict[str, Any] = {}
        try:
            # 压缩是同步重活（摘要拆分 + 近重复两两比对，几百条就要几百毫秒），
            # 放线程池里跑，别把 AstrBot 的事件循环卡住（卡住=所有聊天排队）
            result["compress"] = await asyncio.to_thread(
                self.plugin.compression.compress_memories, bot_id, True
            )
        except Exception as exc:
            result["compress"] = {"error": str(exc)}
        try:
            result["archive"] = self.plugin.compression.archive_stale(bot_id, 90, 0.3, apply=True)
        except Exception as exc:
            result["archive"] = {"error": str(exc)}
        try:
            result["consolidate"] = self.plugin.compression.consolidate_noise(bot_id, apply=True)
        except Exception as exc:
            result["consolidate"] = {"error": str(exc)}
        try:
            self.plugin.cache.cache_set(bot_id, key, "1", ttl=8 * 86400)
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)
        logger.info("[amo_data] 每周记忆整理: %s" % result)
        return result

    async def _maybe_daily_backup(self, bot_id: str) -> str:
        """每天自动备份一次数据库，保留 backup_keep 份。"""
        if not self.settings.get("backup_daily", True):
            return ""
        day = _clock_now().strftime("%Y-%m-%d")
        key = "backup_day:" + day
        try:
            if self.plugin.cache.cache_get(bot_id, key):
                return ""
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)
        path = self.plugin.backup.backup_now(int(self.settings.get("backup_keep", 7) or 7))
        try:
            self.plugin.cache.cache_set(bot_id, key, "1", ttl=2 * 86400)
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)
        if path:
            logger.info("[amo_data] 每日备份: %s" % path)
        return path

    # ---------- 统计 ----------
    def stats(self, bot_id: str = "") -> Dict[str, int]:
        where, params = _bot_filter(bot_id)
        out: Dict[str, int] = {}
        with self.db.lock:
            for name, table in (
                ("profiles", "profiles"), ("memories", "memories"),
                ("relations", "relations"), ("cache", "cache"),
                ("messages", "messages"), ("logs", "logs"), ("persons", "persons"),
            ):
                if where:
                    row = self.db.conn.execute(
                        "SELECT COUNT(*) AS c FROM %s WHERE 1=1%s" % (table, where), params
                    ).fetchone()
                else:
                    row = self.db.conn.execute("SELECT COUNT(*) AS c FROM %s" % table).fetchone()
                out[name] = int(row["c"] or 0) if row else 0
            try:
                for status in ("active", "superseded", "archived", "merged", "dormant", "expired"):
                    row = self.db.conn.execute(
                        "SELECT COUNT(*) AS c FROM memories WHERE COALESCE(status,'active')=?"
                        + (" AND bot_id=?" if where else ""),
                        ([status] + (params if where else [])),
                    ).fetchone()
                    out["memories_" + status] = int(row["c"] or 0) if row else 0
            except Exception as exc:
                logger.debug("[amo_data] optional step failed: %r", exc)
        return out
