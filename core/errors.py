# -*- coding: utf-8 -*-
"""errors.py —— 错误码登记（Phase 17）。

对外 API **仍然返回原来的数字 code**（0 / 1 / 403），这里只做登记与翻译：

    新日志 / health 里带可读的字符串 CODE_*（例如 error_code=db_degraded）
    需要看数字时用 code_number(CODE_FAIL) -> 1 反查，两边一一对应

改协议前先看这张表：CODE_BY_NUMBER / NUMBER_BY_CODE 是面板与插件之间的存量约定，
新增字符串码随便加，已有数字码不许改（tests/test_obs_pii.py 盯着）。
"""
from __future__ import annotations

#: 成功
CODE_OK = "ok"
#: 业务失败（参数缺失、校验不过、下游返回 False）
CODE_FAIL = "fail"
#: 守卫拒绝（非 POST / 跨站 / Origin 不一致）
CODE_FORBIDDEN = "forbidden"
#: 兜底
CODE_UNKNOWN = "unknown"
#: 缺少必填字段
CODE_MISSING_FIELD = "missing_field"
#: 写操作缺确认字段
CODE_CONFIRM_REQUIRED = "confirm_required"
#: 数据库读写失败
CODE_DB_ERROR = "db_error"
#: 数据库降级（schema 版本读不到 / 迁移失败）
CODE_DB_DEGRADED = "db_degraded"
#: 后台任务快照不可用
CODE_JOBS_DEGRADED = "jobs_degraded"
#: 健康检查整体降级（health_snapshot 兜底用）
CODE_HEALTH_DEGRADED = "health.degraded"

#: 存量数字 code（三插件一致：0 成功 / 1 业务失败 / 403 守卫拒绝）
CODE_BY_NUMBER = {0: CODE_OK, 1: CODE_FAIL, 403: CODE_FORBIDDEN}
#: 字符串 -> 数字（反向表由上面一张表生成，保证永远不会两边不一致）
NUMBER_BY_CODE = {name: number for number, name in CODE_BY_NUMBER.items()}

#: 可读说明（日志 / 面板 tooltip 用）
DESCRIPTIONS = {
    CODE_OK: "成功",
    CODE_FAIL: "业务失败",
    CODE_FORBIDDEN: "守卫拒绝（非 POST / 跨站 / Origin 不一致）",
    CODE_UNKNOWN: "未知错误",
    CODE_MISSING_FIELD: "缺少必填字段",
    CODE_CONFIRM_REQUIRED: "写操作缺少确认字段",
    CODE_DB_ERROR: "数据库读写失败",
    CODE_DB_DEGRADED: "数据库降级（schema 版本读不到）",
    CODE_JOBS_DEGRADED: "后台任务快照不可用",
    CODE_HEALTH_DEGRADED: "健康检查降级",
}


def code_name(number, default=CODE_UNKNOWN):
    """数字 code -> 字符串码；不认识的数字给 default。"""
    try:
        return CODE_BY_NUMBER.get(int(number), default)
    except (TypeError, ValueError):
        return default


def code_number(code, default=None):
    """字符串码 -> 数字 code；传数字就原样返回，不认识的给 default。"""
    if isinstance(code, bool):
        return default
    if isinstance(code, (int, float)):
        return int(code)
    return NUMBER_BY_CODE.get(str(code or "").strip(), default)


def is_known(code):
    """这个字符串码登记过没有。"""
    return str(code or "").strip() in DESCRIPTIONS


def describe_code(code):
    """人话说明：数字码 -> 中文含义；字符串码 -> 登记说明；未知的明确说未知。"""
    if isinstance(code, bool):
        return "未知错误码：%s" % code
    if isinstance(code, (int, float)):
        name = code_name(code)
        text = DESCRIPTIONS.get(name, "")
        if not text:
            return "未登记的数字码：%s" % int(code)
        return "%s（code=%d）" % (text, int(code))
    text = str(code or "").strip()
    if not text:
        return "未知错误码：空"
    return DESCRIPTIONS.get(text, "未知错误码：%s" % text)
