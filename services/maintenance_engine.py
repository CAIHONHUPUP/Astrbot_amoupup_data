# -*- coding: utf-8 -*-
"""services/maintenance_engine.py —— 每小时的保留策略调度 + 面板统计。

循环内容与 3.0 一致：压缩老聊天、清过期日志、每周记忆整理、每日备份、向量补全。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

try:
    from ..utils.text_process import _bot_filter, _now, logger
except ImportError:  # 顶层导入（测试/脚本）
    from utils.text_process import _bot_filter, _now, logger


class MaintenanceEngine:
    def __init__(self, plugin):
        self.plugin = plugin
        self.db = plugin.db
        self.settings = plugin.settings
        self._task = None
        self._running = False

    # ---------- 调度 ----------
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 没有事件循环（脚本/单测）就不起后台任务
        try:
            self._task = loop.create_task(self._loop())
            self._running = True
        except Exception:
            self._task = None

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None

    async def _loop(self) -> None:
        """每小时跑一次保留策略。"""
        while True:
            try:
                await asyncio.sleep(3600)
                await self.run_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug("[amo_data] 维护循环异常: %s" % exc)

    async def run_once(self) -> Dict[str, Any]:
        bot_id = self.plugin._bot_id({}, write=True)  # 空 bot_id 会让清理/压缩变成空转
        result: Dict[str, Any] = {}
        try:
            result["retention"] = await self.retention(
                bot_id, self.plugin.settings.chat_days,
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
        return result

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
            result["logs_purged"] = self.plugin.logs.purge(bot_id, int(log_days))
        except Exception as exc:
            result["log_error"] = str(exc)
        return result

    # ---------- 周期任务 ----------
    async def _maybe_weekly_cleanup(self, bot_id: str) -> Dict[str, Any]:
        """每周只跑一次：合并同天摘要、近重复去重、归档低价值老记忆。"""
        import time
        week = time.strftime("%G-W%V", time.localtime())
        key = "cleanup_week:" + week
        try:
            if self.plugin.cache.cache_get(bot_id, key):
                return {"skipped": True}
        except Exception:
            pass
        result: Dict[str, Any] = {}
        try:
            result["compress"] = self.plugin.compression.compress_memories(bot_id, apply=True)
        except Exception as exc:
            result["compress"] = {"error": str(exc)}
        try:
            result["archive"] = self.plugin.compression.archive_stale(bot_id, 90, 0.3, apply=True)
        except Exception as exc:
            result["archive"] = {"error": str(exc)}
        try:
            self.plugin.cache.cache_set(bot_id, key, "1", ttl=8 * 86400)
        except Exception:
            pass
        logger.info("[amo_data] 每周记忆整理: %s" % result)
        return result

    async def _maybe_daily_backup(self, bot_id: str) -> str:
        """每天自动备份一次数据库，保留 backup_keep 份。"""
        if not self.settings.get("backup_daily", True):
            return ""
        import time
        day = time.strftime("%Y-%m-%d", time.localtime())
        key = "backup_day:" + day
        try:
            if self.plugin.cache.cache_get(bot_id, key):
                return ""
        except Exception:
            pass
        path = self.plugin.backup.backup_now(int(self.settings.get("backup_keep", 7) or 7))
        try:
            self.plugin.cache.cache_set(bot_id, key, "1", ttl=2 * 86400)
        except Exception:
            pass
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
                for status in ("active", "superseded", "archived", "merged"):
                    row = self.db.conn.execute(
                        "SELECT COUNT(*) AS c FROM memories WHERE COALESCE(status,'active')=?"
                        + (" AND bot_id=?" if where else ""),
                        ([status] + (params if where else [])),
                    ).fetchone()
                    out["memories_" + status] = int(row["c"] or 0) if row else 0
            except Exception:
                pass
        return out
