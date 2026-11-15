# -*- coding: utf-8 -*-
"""记忆事实守卫：写入前清掉系统日志、虚构通话和自毁倾向。"""
from __future__ import annotations

import re

_SYSTEM = re.compile(r"\[[^\]]*(?:系统|日程|提示|tool|thought|又变)[^\]]*\]"
                     r"|<thought>.*?</thought>", re.S)
_CMD = re.compile(r"(?:^|\s)/[\w\u4e00-\u9fa5]+")
_PHONE = re.compile(r"(?:他|老公)[^。！？\n]{0,8}(?:打来电话|打电话来|打电话|电话(?:马上|立刻)?打过来)")
_EXTREME = re.compile(r"(不想活|自杀|去死|活着没意思|结束生命|撑不下去了?)")


def sanitize_memory_text(text):
    value = _SYSTEM.sub("", str(text or ""))
    value = _CMD.sub("", value)
    value = _PHONE.sub("好想老公", value)
    value = value.replace("阿明", "老公")
    value = _EXTREME.sub("委屈得只想躲进被子里哭一场", value)
    return re.sub(r"\s+", " ", value).strip()
