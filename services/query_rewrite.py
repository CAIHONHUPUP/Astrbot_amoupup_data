# -*- coding: utf-8 -*-
"""services/query_rewrite.py —— 把整句问话改写成能进粗筛的变体（多查询召回）。

amo 那边已经会发几种问法，但用户真正说的话常常带着问句外壳：
「你还记得我上次说的那只猫吗」。整句在库里几乎不可能出现，拆词之前先剥壳：

    original  原句
    core      去掉「你还记得/吗/呢」这类外壳后的核心
    terms     只保留有信息量的二元组（给 token OR 粗筛）

多查询召回：每一路候选各查一次再合并，RRF 名次只用来决定候选顺序，
最终仍由既有打分收敛。纯函数，不碰 DB、不调模型。
"""
from __future__ import annotations

from typing import List

#: 问句前缀（越长的放前面，先剥长的）
PREFIXES = (
    "你还记不记得", "你还记得", "你记不记得", "你记得", "你还记得吗",
    "帮我想想", "帮我回忆", "帮我查一下", "帮我查查", "我想问一下",
    "我想问问", "问你个事", "我问你", "说起来", "对了",
)
#: 问句后缀
SUFFIXES = ("吗", "嘛", "呢", "吧", "呀", "啊")
#: 句末标点/空白
TRIM = " \t\r\n，。,.!！?？~～…"
#: 只剥开头的指代词（出现在中间可能是名字的一部分，不能删）
HEAD_FILLERS = ("那个", "那次", "之前说的", "上次说的")


def _strip_shell(body: str) -> str:
    core = body.strip(TRIM)
    for prefix in PREFIXES:
        if core.startswith(prefix) and len(core) > len(prefix):
            core = core[len(prefix):].strip(TRIM)
            break
    for suffix in SUFFIXES:
        if core.endswith(suffix) and len(core) > len(suffix):
            core = core[:-len(suffix)].strip(TRIM)
            break
    for filler in HEAD_FILLERS:
        if core.startswith(filler) and len(core) > len(filler):
            core = core[len(filler):].strip(TRIM)
            break
    return core


def variants(text: str, max_variants: int = 3) -> List[str]:
    """原句 → 最多 max_variants 个去重变体（原句永远排第一）。"""
    body = str(text or "").strip()
    if not body:
        return []
    limit = max(1, int(max_variants or 1))
    out: List[str] = [body]
    core = _strip_shell(body)
    if core and core not in out:
        out.append(core)
    if len(out) >= limit:
        return out[:limit]
    try:
        from ..utils.text_process import _informative_terms
    except ImportError:  # 顶层导入（测试/脚本）
        from utils.text_process import _informative_terms
    terms = [term for term in _informative_terms(core or body) if len(term) >= 2][:4]
    if len(terms) >= 2:
        joined = " ".join(terms)
        if joined not in out:
            out.append(joined)
    return out[:limit]
