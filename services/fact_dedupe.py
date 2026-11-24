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
    "shopping", "bill", "summary", "log",
}

_SUBJECTS = ("老公", "主人", "用户", "宝贝", "我", "他", "她")
_VERBS = ("最爱吃", "最爱喝", "不喜欢", "不爱吃", "不爱喝", "喜欢吃", "喜欢喝",
          "爱吃", "爱喝", "戒了", "最爱", "喜欢", "讨厌", "爱")
_FILLERS = ("最近", "现在", "其实", "特别", "真的", "比较", "有点", "已经", "一直", "还是", "就")
_NEG = re.compile(r"不(?:喜欢|爱|吃|喝)|戒了|不了|不再|改(?:喝|吃|成)|换成|没那么喜欢")
_PUNCT = re.compile(r"[\[\]【】\s，,。.!！?？~～、；;：:]")
#: 反转判定里不算「共同对象」的二元组：这些只是否定/喜好/换掉的说法本身
_NEG_BIGRAMS = frozenset((
    "我不", "不喜", "不爱", "不喝", "不吃", "不要", "不再", "不了",
    "喜欢", "欢喝", "欢吃", "爱吃", "爱喝", "最爱", "讨厌",
    "改喝", "改吃", "改成", "换成", "那么", "没有", "最近", "现在",
))
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
    """新记忆是否把旧记忆的状态反转了（只标 superseded，不删）。

    2026-11-15 修：以前按「单字集合重叠 >= 2」判定，中文里「他/的/吃/了」几乎必然重叠，
    一句「我不爱吃辣」会把同一个人最近 200 条里大批无关记忆一起标成 superseded，
    等于静默丢记忆。现在要求二元组（相邻两字）真正重合，且占较短一句的 60% 以上。
    """
    if fact_state(new) != "neg":
        return False
    old_key, new_key = fact_key(old), fact_key(new)
    if old_key and old_key == new_key:
        return True
    old_body = _PUNCT.sub("", str(old or ""))
    new_body = _PUNCT.sub("", str(new or ""))
    if len(old_body) < 2 or len(new_body) < 2:
        return False
    a = {old_body[i:i + 2] for i in range(len(old_body) - 1)}
    b = {new_body[i:i + 2] for i in range(len(new_body) - 1)}
    # 只看「有信息的共同对象」：把「我不」「不喜」「改喝」这类否定/换掉的说法剔掉；
    # 至少 2 个共同二元组且占较短句 25%，避免一句「不喜欢草莓」把「买了草莓蛋糕」也归档。
    shared = (a & b) - _NEG_BIGRAMS
    if not shared:
        return False
    needed = max(2, int(0.25 * min(len(a), len(b))))
    return len(shared) >= needed
