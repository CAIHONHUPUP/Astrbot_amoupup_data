# -*- coding: utf-8 -*-
"""services/person_model.py —— 身份归一的三层模型与纯规则（不碰 DB）。

三层语义
    persons        一个人（pid 全局自增，一行一个人）
    person_links   一个平台账号 (bot_id, platform, uid) → pid；主键保证「一个 link 只属于一个 person」
    identity_alias 旧身份 → 规范身份（平台级重定向；只指向 (platform, uid)，不指向 pid）

不变式
    I1 每条 link 的 pid > 0，且 persons 里存在 (bot_id, pid)
    I2 同一个 (bot_id, platform, uid) 只能出现在一条 link 上（表主键兜底）
    I3 每个 (bot_id, pid) 至少有一条 link；没有 link 的 person 是孤儿，由 repair/merge 清理
    I4 alias 必须有目标；alias 指向的是「身份」不是 pid，所以合并只改 pid 不改身份 → alias 自动跟随
    I5 canonical_uid 是只读缓存：link 有任何增删改（resolve/link/unlink/merge/alias）即失效

person_engine 只做 DB 编排；「谁和谁是一个人」「主 uid 选谁」「合并时搬哪些行」
这类判断都在这里，方便单测直接锁定边界。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

try:  # 包模式
    from ..utils.text_process import _name_key
except ImportError:  # 顶层导入（测试/脚本）
    from utils.text_process import _name_key

LARK_PLATFORM = "lark"
MIN_AUTO_MERGE_NAME = 2


def canonical_link(links: Iterable[Dict[str, Any]], fallback: str = "") -> str:
    """同一人选一个主身份：优先飞书 open_id，其次第一条有效 uid。"""
    items = [item for item in (links or []) if str(item.get("uid") or "")]
    for item in items:
        if str(item.get("platform") or "") == LARK_PLATFORM:
            return str(item["uid"])
    for item in items:
        return str(item["uid"])
    return str(fallback or "")


def name_key(name: Any) -> str:
    """名字归一键（纯函数，供 resolve 的同名自动合并使用）。"""
    return _name_key(name)


def auto_merge_target(rows: Iterable[Dict[str, Any]], display: Any,
                      current_platform: str = "") -> int:
    """同名自动合并：返回唯一命中的 pid；没有命中或命中多人返回 0（宁可不并）。"""
    key = _name_key(display)
    if len(key) < MIN_AUTO_MERGE_NAME:
        return 0
    pids = set()
    for item in rows or []:
        pid = int(item.get("pid") or 0)
        if pid <= 0:
            continue
        if current_platform and str(item.get("platform") or "") == current_platform:
            continue
        if _name_key(item.get("name")) == key:
            pids.add(pid)
    return pids.pop() if len(pids) == 1 else 0


def ghost_is_mergeable(profile_hits: int, memory_count: int, other_message_count: int,
                       total_message_count: int) -> bool:
    """幽灵人物判定：有画像/有记忆/有真人对话都不并；一条消息都没有的空人物也不并。

    空人物往往只是刚绑好的身份（resolve/link 刚建、还没聊过），并掉会把用户
    刚建的人吞了；真正的幽灵一定有摘要之类挂在自己名下。
    """
    if int(profile_hits or 0) > 0 or int(memory_count or 0) > 0:
        return False
    if int(other_message_count or 0) > 0:
        return False
    return int(total_message_count or 0) > 0


def unlink_message_scope(same_platform_links: int) -> str:
    """解绑时按什么搬消息：该平台只剩这一条 link → 按平台；同平台还有别的 link → 只按 uid。"""
    return "platform" if int(same_platform_links or 0) <= 1 else "uid"


def person_is_empty(links_left: int, messages_left: int, memories_left: int) -> bool:
    """解绑/合并后能不能删 person：还有任何一行引用它就不能删，否则 pid 会悬空。"""
    return (int(links_left or 0) == 0 and int(messages_left or 0) == 0
            and int(memories_left or 0) == 0)


def invariants(persons: Iterable[Dict[str, Any]], links: Iterable[Dict[str, Any]],
               aliases: Iterable[Dict[str, Any]] = ()) -> List[str]:
    """检查三层身份模型的不变式，返回问题列表（空列表 = 一致）。只读、无副作用。"""
    issues: List[str] = []
    person_keys = set()
    for row in persons or []:
        pid = int(row.get("pid") or 0)
        if pid <= 0:
            issues.append("person pid<=0: %r" % (row,))
            continue
        person_keys.add((str(row.get("bot_id") or ""), pid))
    seen_identities = set()
    linked_keys = set()
    for row in links or []:
        bot = str(row.get("bot_id") or "")
        pid = int(row.get("pid") or 0)
        identity = (bot, str(row.get("platform") or ""), str(row.get("uid") or ""))
        if identity in seen_identities:
            issues.append("I2 duplicate link: %r" % (identity,))
        seen_identities.add(identity)
        if pid <= 0:
            issues.append("I1 link pid<=0: %r" % (identity,))
        elif (bot, pid) not in person_keys:
            issues.append("I1 dangling pid: %r -> %d" % (identity, pid))
        linked_keys.add((bot, pid))
    for key in sorted(person_keys - linked_keys):
        issues.append("I3 person without link: %r" % (key,))
    for row in aliases or []:
        target = (str(row.get("alias_platform") or ""), str(row.get("alias_uid") or ""))
        if not target[0] or not target[1]:
            issues.append("I4 alias without target: %r" % (row,))
    return issues
