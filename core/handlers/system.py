# -*- coding: utf-8 -*-
"""core/handlers/system.py —— 面板系统接口（/status /backup/* /logs /schedule /maintenance）。"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

try:  # 包模式
    from ...utils.config import PLUGIN_VERSION
    from ..observability import health_snapshot
    from ..web_response import confirm_required, confirmed, fail, int_field, missing, ok
except ImportError:  # 顶层导入（测试）
    from utils.config import PLUGIN_VERSION
    from core.observability import health_snapshot
    from core.web_response import confirm_required, confirmed, fail, int_field, missing, ok


#: 插件载入时刻，给 /status 的 health.uptime_s 当起点
_OBS_STARTED_AT = time.time()


class SystemHandlersMixin:
    """面板系统面：状态、备份、日志、缓存（日程/世界观）、保留策略。"""

    if TYPE_CHECKING:  # 宿主属性/方法由 main.py 的其他模块提供
        _body: Any
        _bot_id: Any
        _cfg_int: Any
        cache: Any
        chat_days: Any
        compress_after: Any
        db: Any
        db_path: Any
        log_days: Any
        logs: Any
        maintenance: Any
        mem: Any
        memory: Any
        messages: Any
        refresh_config: Any
        settings: Any

    async def api_status(self):
        self.refresh_config()
        payload = await self._body()
        bot_id = self._bot_id(payload)
        counts = self.maintenance.stats(bot_id)
        return ok({
            "version": PLUGIN_VERSION,
            "bot_id": bot_id,
            "db": self.db_path,
            "counts": counts,
            "jobs": self.maintenance.jobs.report(),
            # Phase 17：健康快照只新增字段（plugin/version/uptime/db/jobs/counts）
            # Phase 19：跨插件记忆 API 协议摘要（只新增字段）
            "protocol": self.mem.protocol_info(),
            "health": health_snapshot(
                "Astrbot_amoupup_data",
                version=PLUGIN_VERSION,
                db=getattr(self.db, "schema_report", None) or {},
                jobs=self.maintenance.jobs,
                counts=counts,
                started_at=_OBS_STARTED_AT,
                now=time.time(),
            ),
            "bots": self.db.bot_ids(),
            "embedding": self.memory.embedding_stats(),
            "recall": {
                "pool_size": self.settings.get("pool_size"),
                "min_matched_terms": self.settings.get("min_matched_terms"),
                "half_life_days": self.settings.get("forget_half_life_days"),
                "auto_dedupe": self.settings.get("auto_dedupe"),
            },
        })

    async def api_backup_now(self):
        payload = await self._body()
        keep = payload.get("keep", self._cfg_int("backup_keep", 7))
        path = await self.mem.backup_now(int(keep))
        if not path:
            return fail("备份失败")
        return ok({"path": path, "name": os.path.basename(path),
                   "items": await self.mem.list_backups()})

    async def api_backup_list(self):
        return ok({"items": await self.mem.list_backups()})

    async def api_backup_export(self):
        payload = await self._body()
        snapshot = await self.mem.export_snapshot(payload.get("bot_id") or "")
        return ok({"snapshot": snapshot})

    async def api_backup_import(self):
        payload = await self._body()
        snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
        if not isinstance(snapshot, dict):
            return fail("缺少 snapshot")
        return ok(await self.mem.import_snapshot(snapshot))

    async def api_backup_restore(self):
        payload = await self._body()
        name = str(payload.get("name") or "").strip()
        if not name:
            return fail("缺少备份文件名")
        counts = await self.mem.restore_backup(name, payload.get("bot_id") or "")
        if isinstance(counts, dict) and counts.get("error"):
            return fail("备份文件不存在或损坏")
        return ok(counts)

    async def api_logs(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        level = str(payload.get("level") or "").strip()
        limit = payload.get("limit", 200)
        return ok({"items": self.logs.list_logs(bot_id, level, limit)})

    async def api_log_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        log_id = int_field(payload, "id")
        if not log_id:
            return missing("id")
        return ok({"deleted": self.logs.delete_log(bot_id, log_id)})

    async def api_log_clear(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        if not confirmed(payload):
            return confirm_required("清空")
        return ok({"deleted": self.logs.clear_logs(bot_id)})

    async def api_schedule(self):
        """日程/世界观这类缓存：列出 key 和大小，点进去看内容。"""
        payload = await self._body()
        bot_id = self._bot_id(payload)
        key = str(payload.get("key") or "").strip()
        if key:
            return ok({"key": key, "value": self.cache.cache_get(bot_id, key)})
        items = []
        for name in self.cache.cache_keys(bot_id):
            value = self.cache.cache_get(bot_id, name)
            size = 0
            try:
                size = len(json.dumps(value, ensure_ascii=False))
            except Exception:
                size = 0
            items.append({"key": name, "size": size})
        return ok({"items": items})

    async def api_schedule_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        key = str(payload.get("key") or "").strip()
        if not key:
            return missing("key")
        return ok({"deleted": self.cache.cache_delete(bot_id, key)})

    async def api_maintenance(self):
        """手动跑一次保留策略：压缩老聊天 + 删过期日志。"""
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        result: dict[str, Any] = {}
        try:
            result.update(self.messages.compress_messages(bot_id, self.compress_after, self.chat_days))
        except Exception as exc:
            result["compress_error"] = str(exc)
        try:
            result["logs_purged"] = self.logs.purge_logs(bot_id, self.log_days)
        except Exception as exc:
            result["log_error"] = str(exc)
        return ok(result)
