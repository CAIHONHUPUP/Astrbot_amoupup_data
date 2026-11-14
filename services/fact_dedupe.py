# -*- coding: utf-8 -*-
"""事实级去重（2026-11-14）：偏好/状态合并，流水账绝不去重。

- is_static_kind()：只有偏好、人设、关系、资产这类静态事实参与去重；
  消费/交易/消息/摘要/日记这些流水账永远保留每一条。
- fact_key()：把「我喜欢吃螺蛳粉」「螺蛳粉真好吃」「我最爱吃螺蛳粉」归一成同一个键；
- fact_state()：正/负状态（不喜欢、戒了、改喝…）；
- merge_fact()：同一事实的细节合并（螺蛳粉 + 加辣的 → 螺蛳粉，尤其是加辣的）；
- should_expire()：新状态是反转时，旧状态该标 expired，不物理删除。
"""
from __future__ import annotations

import re

STATIC_KINDS = {
    "fact", "preference", "preference_fact", "profile", "persona", "setting",
    "relation", "relationship", "asset", "identity", "habit", "state",
}
NON_STATIC_KINDS = {
    "expense", "income", "trade", "transaction", "message", "conversation_summary",
    "daily_digest", "diary", "event", "milestone", "bot_media", "voice_note",
    "shopping", "bill", "summary", "log", "finance",
}

_SUBJECTS = ("老公", "主人", "用户", "宝贝", "我", "他", "她")
_VERBS = ("最爱吃", "最爱喝", "不喜欢", "不爱吃", "不爱喝", "喜欢吃", "喜欢喝",
          "爱吃", "爱喝", "戒了", "最爱", "喜欢", "讨厌", "爱")
_FILLERS = ("最近", "现在", "其实", "特别", "真的", "比较", "有点", "已经", "一直", "还是", "就")
_NEG = re.compile(r"不(?:喜欢|爱|吃|喝)|戒了|不了|不再|改(?:喝|吃|成)|换成|没那么喜欢")
_PUNCT = re.compile(r"[\[\]【】\s，,。.!！?？~～、；;：:]")
_MODIFIERS = ("加辣", "微辣", "中辣", "特辣", "辣的", "冰的", "热的",
              "大杯", "小杯", "少糖", "多糖", "半糖")
_OBJ_FIRST = re.compile(r"^(?P<obj>[^，。！？]{2,12}?)(?:真|很|超|太|特别|挺)?(?:好吃|好喝|不错|香)$")


def is_static_kind(kind):
    body = str(kind or "").strip().lower()
    if body in NON_STATIC_KINDS:
        return False
    return body in STATIC_KINDS


def _strip_prefix(text):
    body = str(text or "").strip()
    body = re.sub(r"^\[[^\]]*\]\s*", "", body)
    body = re.sub(r"^【[^】]*】\s*", "", body)
    return body.strip()


def pref_parts(text):
    """拆出 (主体, 动词, 宾语)；不是偏好句返回 None。"""
    body = _strip_prefix(text)
    if not body:
        return None
    subject = ""
    for word in _SUBJECTS:
        if body.startswith(word):
            subject = word
            body = body[len(word):].strip()
            break
    for filler in _FILLERS:
        if body.startswith(filler):
            body = body[len(filler):].strip()
    verb = ""
    for word in _VERBS:
        if body.startswith(word):
            verb = word
            body = body[len(word):].strip()
            break
    if not verb:
        return None
    obj = _PUNCT.sub("", body)
    if not obj:
        return None
    return subject, verb, obj


def _subject_key(subject):
    return "self" if str(subject or "") in ("我", "他", "她", "老公", "主人", "用户", "宝贝") else "any"


def _obj_key(obj):
    body = str(obj or "")
    for filler in _FILLERS:
        body = body.replace(filler, "")
    for modifier in _MODIFIERS:
        body = body.replace(modifier, "")
    body = re.sub(r"^的+", "", body)
    body = re.sub(r"^(?:吃|喝)", "", body)
    return _PUNCT.sub("", body)


def fact_key(text):
    """静态事实归一键；非偏好句返回空串。"""
    parts = pref_parts(text)
    if parts:
        subject, _verb, obj = parts
        key = _obj_key(obj)
        if key:
            return "pref:%s:%s" % (_subject_key(subject), key)
    body = _strip_prefix(text)
    match = _OBJ_FIRST.match(body)
    if match:
        key = _obj_key(match.group("obj"))
        if key:
            return "pref:self:" + key
    return ""


def fact_state(text):
    return "neg" if _NEG.search(str(text or "")) else "pos"


def merge_fact(old, new):
    """同一事实的细节合并；不相关就保留更完整的那条。"""
    old = str(old or "").strip()
    new = str(new or "").strip()
    if not old:
        return new
    if not new:
        return old
    op, np_ = pref_parts(old), pref_parts(new)
    if op and np_ and _obj_key(op[2]) == _obj_key(np_[2]):
        subject = np_[0] or op[0]
        verb = np_[1] or op[1]
        old_obj, new_obj = op[2], np_[2]
        if old_obj in new_obj:
            extra = new_obj.replace(old_obj, "").strip("的 ")
            if extra and extra not in old_obj:
                return "%s%s%s（%s）" % (subject, verb, old_obj, extra)
            return new
        if new_obj in old_obj:
            return old
    if new in old:
        return old
    if old in new:
        return new
    return old + "；" + new


def should_expire(old, new):
    """新记忆是否把旧记忆的状态反转了（只标 expired，不删）。"""
    if fact_state(new) != "neg":
        return False
    old_key, new_key = fact_key(old), fact_key(new)
    if old_key and old_key == new_key:
        return True
    a = set(_PUNCT.sub("", str(old or "")))
    b = set(_PUNCT.sub("", str(new or "")))
    return len(a & b) >= 2
