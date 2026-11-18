# -*- coding: utf-8 -*-
"""core/web_response.py —— 面板接口的统一响应封装（Phase 13 P2）。

先统计现状（core/web_handlers.py 34 个 handler + 守卫）再定约定，不做破坏性改名：
    成功  {"code": 0, "data": <任意>, ["msg": "已保存"], ["applied": bool]}
    失败  {"code": 1, "msg": "<原因>"}
    守卫  {"code": 403, "msg": "..."}（非 POST / 跨站 / Origin 不一致）

所以这里只把构造收口：新代码一律 ok()/fail()/result()，参数校验用
text_field()/int_field()/flag_field()/confirmed() + missing()/confirm_required()。
失败码集中在 CODE_OK / CODE_FAIL / CODE_FORBIDDEN，避免各 handler 各写一个数字。
既有响应的字段与文案逐字不变（有 tests/test_api_coverage2.py 盯着）。
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

#: 成功
CODE_OK = 0
#: 业务失败（参数缺失、校验不过、下游返回 False）
CODE_FAIL = 1
#: 守卫拒绝（非 POST / 跨站 / Origin 不一致）
CODE_FORBIDDEN = 403

#: 写操作确认字段的接受值（面板传 confirm=1）
_TRUE_WORDS = ("1", "true", "yes")


def ok(data: Any = None, msg: str = "", **extra: Any) -> Dict[str, Any]:
    """成功响应：code=0 + data（可选 msg / 透传附加字段）。"""
    body: Dict[str, Any] = {"code": CODE_OK, "data": data}
    if msg:
        body["msg"] = msg
    for key, value in extra.items():
        body[key] = value
    return body


def fail(msg: str, code: int = CODE_FAIL, **extra: Any) -> Dict[str, Any]:
    """失败响应：code=1/403 + msg。"""
    body: Dict[str, Any] = {"code": int(code), "msg": str(msg)}
    for key, value in extra.items():
        body[key] = value
    return body


def result(flag: Any, data: Any = None, msg_ok: str = "", msg_fail: str = "") -> Dict[str, Any]:
    """下游返回 True/False 的一步到位封装。

    失败时若有 data（例如 {"ok": False}）一并带回，与原实现的字段保持一致。
    """
    if flag:
        return ok(data, msg_ok)
    return fail(msg_fail, data=data) if data is not None else fail(msg_fail)


def forbidden(msg: str) -> Dict[str, Any]:
    """守卫拒绝（403）。"""
    return fail(msg, CODE_FORBIDDEN)


_UNSET = object()


def missing(*fields: str, msg: str = "") -> Dict[str, Any]:
    """参数缺失：默认文案「缺少 xxx」。"""
    return fail(msg or ("缺少 " + "、".join(fields)))


def confirm_required(action: str = "") -> Dict[str, Any]:
    """写操作需要显式确认。"""
    return fail("%s需要确认：confirm=1" % (action or "操作"))


def text_field(payload: Dict[str, Any], *keys: str) -> str:
    """取第一个非空文本字段（去首尾空白）；都没有返回空串。"""
    data = payload or {}
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def int_field(payload: Dict[str, Any], key: str, default: int = 0) -> int:
    """取整数字段；非法值退回 default（面板传字符串很常见）。"""
    try:
        return int((payload or {}).get(key) or 0)
    except (TypeError, ValueError):
        return int(default or 0)


def flag_field(payload: Dict[str, Any], key: str, default: bool = True) -> bool:
    """布尔开关：接受 true/false、1/0、"0"/"false"/"no"/"off"。"""
    value = (payload or {}).get(key, default)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off")
    return bool(value)


def confirmed(payload: Dict[str, Any], key: str = "confirm") -> bool:
    """写操作是否显式确认（confirm=1/true/yes）。"""
    return str((payload or {}).get(key) or "") in _TRUE_WORDS


def normalize(response: Any) -> Dict[str, Any]:
    """守卫兜底：保证返回一定是对象、code 一定是整数。

    只补缺不覆盖：成功补 data，失败补 msg；handler 已经给的值一个不动。
    """
    if not isinstance(response, dict):
        return fail("接口返回了非对象结果")
    body = dict(response)
    try:
        body["code"] = int(body.get("code", CODE_FAIL))
    except (TypeError, ValueError):
        body["code"] = CODE_FAIL
    if body["code"] == CODE_OK:
        body.setdefault("data", None)
    else:
        if not body.get("msg"):
            body["msg"] = "请求失败"
        if body["code"] == CODE_FORBIDDEN:
            body.pop("data", None)
    return body
