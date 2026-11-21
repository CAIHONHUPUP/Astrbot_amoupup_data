# -*- coding: utf-8 -*-
"""services/memory_schema.py —— 记忆写入的唯一校验/归一化路径（纯函数，不碰 DB）。

写一条记忆前，content / kind / scope（分层 tier）/ importance / idem_key / tags /
subject / 时间戳全部经过 prepare_write()；memory_engine.remember 不再自己拼一套，
fact_dedupe（去重键、静态事实判定）与 fact_guard（正文清洗）只作为纯工具被这里调用。

字段规则
    kind        去首尾空白、限 32 字；空 -> 'fact'（不拒绝未知 kind：历史数据兼容）
    scope/tier  'group' 才算群聊；其它一律 private；private 时 group_id 清空
    importance  夹到 0~1；非数字按 0.5
    idem_key    去空白、限 120 字；只有静态事实在 auto_dedupe 打开时自动生成
    tags        折叠空白、限 200 字；空串表示「不改旧标签」（DAO 的 TAGS_KEEP_SQL）
    subject     大小写不敏感地只认 'bot'，其余归 'user'
    text        event/activity/diary/conversation_summary/daily_digest 先过 fact_guard 清洗

idem 冲突语义
    同一个 (bot_id, uid, scope, group_id, idem_key) 视为同一件事：
    - active 行存在：静态事实走 merge_fact 合并细节；非静态（事件/流水）以最新正文覆盖，
      keep_existing_content=True 时保留旧正文；两种都抬 importance、刷新 last_hit_at；
    - 旧行已归档/取代/合并：复活或并回旧行（memory_engine._try_merge 负责），不静默丢写。
"""
from __future__ import annotations

from typing import Any, Dict

try:  # 包模式
    from ..services import fact_dedupe
    from ..services.fact_guard import sanitize_memory_text
    from ..utils.text_process import _clean, _norm_key, _norm_tags
except ImportError:  # 顶层导入（测试/脚本）
    from services import fact_dedupe
    from services.fact_guard import sanitize_memory_text
    from utils.text_process import _clean, _norm_key, _norm_tags

KIND_LIMIT = 32
IDEM_LIMIT = 120
TAGS_LIMIT = 200
CONTENT_LIMIT = 2000

#: 需要先过 fact_guard 的「流水账」类正文（系统日志/虚构通话/自毁倾向清洗）
SANITIZE_KINDS = frozenset((
    "event", "activity", "diary", "conversation_summary", "daily_digest",
))


def normalize_kind(kind: Any) -> str:
    """kind 归一化：去空白、限长；空值按 'fact'。"""
    return _clean(kind, KIND_LIMIT) or "fact"


def is_static_kind(kind: Any) -> bool:
    """静态事实判定（去重/取代用）；定义仍在 fact_dedupe，这里只做唯一入口转发。"""
    return fact_dedupe.is_static_kind(kind)


def fact_key(text: Any) -> str:
    """事实归一键（只对静态事实有意义）；定义在 fact_dedupe。"""
    return fact_dedupe.fact_key(text)


def should_sanitize(kind: Any) -> bool:
    """这条 kind 的正文要不要过 fact_guard。"""
    return normalize_kind(kind) in SANITIZE_KINDS


def normalize_subject(subject: Any) -> str:
    """subject 只认 'bot'，其余（含空）都归 'user'。"""
    return "bot" if str(subject or "").strip().lower() == "bot" else "user"


def normalize_scope(scope: Any, group_id: Any):
    """scope 归一化：只有 'group' 算群聊；private 时 group_id 必须清空。"""
    value = "group" if str(scope or "") == "group" else "private"
    group = str(group_id or "") if value == "group" else ""
    return value, group


def normalize_importance(value: Any) -> float:
    """importance 夹到 0~1；非数字按 0.5。"""
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def normalize_idem_key(value: Any) -> str:
    """显式 idem_key 去空白、限长。"""
    return _clean(value, IDEM_LIMIT)


def normalize_times(created_at: Any, last_hit_at: Any, hit_count: Any,
                    now: int) -> Dict[str, int]:
    """时间戳归一：created_at 缺省 now，last_hit_at 缺省 created_at，hit_count >= 0。"""
    try:
        created = int(created_at) if created_at else int(now)
    except (TypeError, ValueError):
        created = int(now)
    try:
        hit = int(last_hit_at) if last_hit_at else created
    except (TypeError, ValueError):
        hit = created
    try:
        count = max(0, int(hit_count or 0))
    except (TypeError, ValueError):
        count = 0
    return {"created_at": created, "last_hit_at": hit, "hit_count": count}


def prepare_write(content: Any, kind: Any = "fact", importance: Any = 0.5,
                  scope: Any = "private", group_id: Any = "", subject: Any = "user",
                  idem_key: Any = "", tags: Any = "", auto_dedupe: bool = True,
                  created_at: Any = None, last_hit_at: Any = None,
                  hit_count: Any = 0, now: int = 0) -> Dict[str, Any]:
    """写入前的唯一校验/归一化入口；返回 {'ok': True, ...归一化字段} 或 {'ok': False, 'reason'}。"""
    text = _clean(content, CONTENT_LIMIT)
    if not text:
        return {"ok": False, "reason": "empty"}
    kind = normalize_kind(kind)
    if should_sanitize(kind):
        text = _clean(sanitize_memory_text(text), CONTENT_LIMIT)
        if not text:
            return {"ok": False, "reason": "empty"}
    static = is_static_kind(kind)
    fact = fact_key(text) if static else ""
    scope, group_id = normalize_scope(scope, group_id)
    subject = normalize_subject(subject)
    idem_key = normalize_idem_key(idem_key)
    if not idem_key and auto_dedupe and static:
        # 只有静态事实才自动生成去重键；消费/交易/流水账每次都是真实发生，必须逐条存
        idem_key = ("fact:" + fact) if fact else _norm_key(text)
    times = normalize_times(created_at, last_hit_at, hit_count, now)
    return {
        "ok": True,
        "text": text,
        "kind": kind,
        "static": static,
        "fact": fact,
        "importance": normalize_importance(importance),
        "scope": scope,
        "group_id": group_id,
        "subject": subject,
        "idem_key": idem_key,
        "tags": _norm_tags(tags, TAGS_LIMIT),
        "created_at": times["created_at"],
        "last_hit_at": times["last_hit_at"],
        "hit_count": times["hit_count"],
    }
