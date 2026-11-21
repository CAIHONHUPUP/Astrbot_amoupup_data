# -*- coding: utf-8 -*-
"""core/handlers/base.py —— 面板 handler 的公共底座（请求体 / 陪伴侧同步删画像）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from astrbot.api import logger

try:  # 包模式
    from ..web_request import web_request
except ImportError:  # 顶层导入（测试）
    from core.web_request import web_request

try:  # Phase 17：结构化事件日志（自动脱敏）
    from ..errors import CODE_FAIL
    from ..observability import log_event
except ImportError:  # 顶层导入（测试）
    from core.errors import CODE_FAIL
    from core.observability import log_event


class HandlerBaseMixin:
    """34 条面板接口共用的两件事：读 JSON 请求体、删画像时同步陪伴插件。"""

    if TYPE_CHECKING:  # 宿主插件实例在 main.py 里拼装，这里只声明类型
        context: Any

    def _amo_plugin(self):
        """拿陪伴插件实例（没装/没加载就返回 None），用于同步删除画像。"""
        try:
            getter = getattr(self.context, "get_registered_star", None)
            meta = None
            if callable(getter):
                for _name in ("Astrbot_amoupup_forever", "Astrbot_Amo_forever"):
                    try:
                        meta = getter(_name)
                    except Exception:
                        meta = None
                    if meta is not None:
                        break
            return getattr(meta, "star_cls", None) if meta is not None else None
        except Exception as exc:
            logger.debug("[amo_data] 查找陪伴插件失败: %s" % exc)
            return None

    def _forget_in_amo(self, uid: str) -> bool:
        """把画像也从陪伴插件本地删掉，否则它 10 分钟一次的画像对账会把它推回来。"""
        amo = self._amo_plugin()
        db = getattr(amo, "db", None) if amo is not None else None
        remover = getattr(db, "delete_profile", None)
        if not callable(remover):
            return False
        try:
            remover(str(uid))
            log_event("info", "profile.amo_delete.synced", uid=uid)
            return True
        except Exception as exc:
            log_event("warning", "profile.amo_delete.failed", uid=uid,
                      detail=str(exc), error_code=CODE_FAIL)
            return False

    async def _body(self) -> dict:
        """读请求体；解析失败一律当成空字典（面板 GET 也要能跑）。"""
        try:
            if web_request is not None and hasattr(web_request, "json"):
                data = await web_request.json(default={})
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)
        try:
            if web_request is not None:
                data = await web_request.get_json(silent=True)
                return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)
        return {}
