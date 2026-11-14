# -*- coding: utf-8 -*-
"""core/web_api.py —— 路由表 + 写接口守卫 + 注册/清理。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Optional

from astrbot.api import logger

try:  # 包模式
    from .web_request import web_request
except ImportError:  # 顶层导入（测试）
    from core.web_request import web_request

try:  # 顶层导入（测试）
    from ..utils.config import PLUGIN_NAME, PLUGIN_VERSION
except ImportError:
    from utils.config import PLUGIN_NAME, PLUGIN_VERSION


try:
    from .web_handlers import WebHandlersMixin
except ImportError:
    from core.web_handlers import WebHandlersMixin

class WebAPIMixin(WebHandlersMixin):
    """面板路由注册；写接口只收 POST 并挡跨站请求。"""

    WRITE_PATHS = frozenset({
        "/profile/save", "/profile/delete",
        "/person/link", "/person/unlink", "/person/merge",
        "/memory/save", "/memory/delete", "/memory/maintain", "/memory/backfill_platform",
        "/relation/save", "/relation/delete",
        "/backup/now", "/backup/import", "/backup/restore",
        "/message/delete", "/message/clear", "/group/rename",
        "/log/delete", "/log/clear",
        "/schedule/delete", "/maintenance",
    })

    def _guard(self, handler, is_write=False):
        """异常兜底 + 写接口的前置检查（只收 POST、拒绝跨站）。

        不是完整鉴权（AstrBot 侧有自己的登录态），只挡浏览器里最容易发生的 CSRF：
        ``<img src>``、聊天预览卡片、外链页面的跨站表单。
        """
        async def wrapper(*args, **kwargs):
            try:
                if is_write:
                    method = ""
                    origin = ""
                    host = ""
                    site = ""
                    headers = None
                    if web_request is not None:
                        method = str(getattr(web_request, "method", "") or "").upper()
                        headers = getattr(web_request, "headers", None)
                        if headers is not None:
                            origin = str(headers.get("origin") or headers.get("referer") or "")
                            host = str(headers.get("host") or "")
                            site = str(headers.get("sec-fetch-site") or "").strip().lower()
                    if method and method != "POST":
                        logger.warning("[amo_data] 拒绝非 POST 的 Web 写操作（%s）" % method)
                        return {"code": 403, "msg": "写操作只接受 POST（面板请刷新到最新版）"}
                    if site == "cross-site":
                        logger.warning("[amo_data] 拒绝跨站 Web 写操作（origin=%s）" % origin)
                        return {"code": 403, "msg": "拒绝跨站请求"}
                    if origin and host:
                        from urllib.parse import urlparse
                        origin_host = (urlparse(origin).hostname or "").lower()
                        allowed = {(host.split(":")[0] or "").strip().lower()}
                        if headers is not None:
                            for key in ("x-forwarded-host", "x-real-host"):
                                value = str(headers.get(key) or "").split(",")[0].strip()
                                if value:
                                    allowed.add(value.split(":")[0].strip().lower())
                        allowed.discard("")
                        if origin_host and allowed and origin_host not in allowed:
                            if site in ("same-origin", "same-site"):
                                logger.debug("[amo_data] Web 写操作 Host 与 Origin 不一致（同源，放行）")
                            else:
                                logger.warning("[amo_data] 拒绝来源不一致的 Web 写操作: %s -> %s"
                                               % (origin, host))
                                return {"code": 403, "msg": "拒绝来源不一致的请求"}
            except Exception as exc:
                logger.debug("[amo_data] Web 守卫检查失败（放行）: %s" % exc)
            try:
                return await handler(*args, **kwargs)
            except Exception as exc:
                logger.error("[amo_data] Web API 异常: %s" % exc)
                return {"code": 1, "msg": str(exc)}
        return wrapper


    def _safe_register_web_apis(self) -> None:
        # 清掉上一实例/旧版本残留的路由：AstrBot 重载插件时不会清理
        # registered_web_apis，而 register_web_api 只在「路由 + methods 完全相同」时替换。
        # 旧条目是 [GET, POST]，新注册是 [POST]，不清理就会两份并存、GET 仍打到旧 handler
        # （实测：写接口改成 POST-only 后，GET /backup/now 照样执行并写了备份文件）。
        try:
            prefixes = [PLUGIN_NAME, getattr(self, "name", PLUGIN_NAME)]
            own = tuple(
                "/" + str(prefix).strip("/") + "/"
                for prefix in prefixes if str(prefix).strip("/")
            )
            holders = (type(self.context), self.context)
            seen_holders = set()
            for holder in holders:
                if id(holder) in seen_holders:
                    continue
                seen_holders.add(id(holder))
                registry = getattr(holder, "registered_web_apis", None)
                if not isinstance(registry, list):
                    continue
                registry[:] = [
                    item for item in registry
                    if not (
                        isinstance(item, tuple) and item
                        and str(item[0]).startswith(own)
                    )
                ]
        except Exception as exc:
            logger.warning("[amo_data] 清理旧 Web API 路由失败（重载后请完整重启）: %s" % exc)
        routes = [
            ("/status", self.api_status),
            ("/profiles", self.api_profiles),
            ("/profile", self.api_profile),
            ("/profile/save", self.api_profile_save),
            ("/profile/delete", self.api_profile_delete),
            ("/persons", self.api_persons),
            ("/person", self.api_person),
            ("/person/link", self.api_person_link),
            ("/person/unlink", self.api_person_unlink),
            ("/person/merge", self.api_person_merge),
            ("/memories", self.api_memories),
            ("/memory/save", self.api_memory_save),
            ("/memory/delete", self.api_memory_delete),
            ("/memory/maintain", self.api_memory_maintain),
            ("/memory/backfill_platform", self.api_memory_backfill_platform),
            ("/relations", self.api_relations),
            ("/relation/save", self.api_relation_save),
            ("/relation/delete", self.api_relation_delete),
            ("/backup/now", self.api_backup_now),
            ("/backup/list", self.api_backup_list),
            ("/backup/export", self.api_backup_export),
            ("/backup/import", self.api_backup_import),
            ("/backup/restore", self.api_backup_restore),
            ("/messages", self.api_messages),
            ("/groups", self.api_groups),
            ("/group/rename", self.api_group_rename),
            ("/message/delete", self.api_message_delete),
            ("/message/clear", self.api_message_clear),
            ("/logs", self.api_logs),
            ("/log/delete", self.api_log_delete),
            ("/log/clear", self.api_log_clear),
            ("/schedule", self.api_schedule),
            ("/schedule/delete", self.api_schedule_delete),
            ("/maintenance", self.api_maintenance),
        ]
        prefixes = [PLUGIN_NAME, getattr(self, "name", PLUGIN_NAME)]
        seen = set()
        registered = 0
        for prefix in prefixes:
            clean = str(prefix or "").strip("/")
            if not clean or clean in seen:
                continue
            seen.add(clean)
            for path, handler in routes:
                is_write = path in self.WRITE_PATHS
                methods = ["POST"] if is_write else ["GET", "POST"]
                try:
                    self.context.register_web_api(
                        "/" + clean + path, self._guard(handler, is_write), methods, path
                    )
                except Exception as exc:
                    logger.warning("[amo_data] 注册 %s%s 失败: %s" % (clean, path, exc))
                else:
                    registered += 1
        if registered:
            logger.info("[amo_data] Web 面板接口已注册 %d 条" % registered)
        else:
            logger.warning("[amo_data] Web 面板接口一条都没注册成功，面板会显示“后端未就绪”")

