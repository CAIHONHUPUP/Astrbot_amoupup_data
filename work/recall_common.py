# -*- coding: utf-8 -*-
"""召回评估公共件：桩安装、设置映射、时钟冻结、注入渲染、判分。

只被 work/*.py 与 tests/test_recall_quality.py 复用；不写真实库。
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)
_TESTS = os.path.join(PLUGIN_ROOT, "tests")
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

#: 真实库路径：评估代码只读它（backup 拷贝），任何写入都必须指到副本。
REAL_DB = r"D:\dafeiyu\astrbot\data\plugin_data\Astrbot_amoupup_data\amo_data.db"
DEFAULT_CONFIG = r"D:\dafeiyu\astrbot\data\config\Astrbot_amoupup_data_config.json"

#: 注入块抬头（amo 侧把匹配到的记忆贴进 system prompt 的格式）
INJECT_HEADER = "【关于你们之间的记忆】"
#: 单条记忆截断上限（字符）。120 是保守值：6 条 x 120 + 抬头 ≈ 760 字。
INJECT_ITEM_MAX = 120


def install_stub():
    """装上 astrbot 最小桩，让 services/* 能 import。幂等（走 work/sim_lib.py）。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sim_lib import install_stub as _install
    _install()


#: 配置键 -> 引擎内部设置键（与 utils/config.py 的映射保持一致）
CONFIG_MAP = {
    "pool_size": ("recall_pool_size", int, 400),
    "recall_recent_pool": ("recall_recent_pool", int, 600),
    "min_matched_terms": ("recall_min_matched_terms", int, 1),
    "forget_half_life_days": ("recall_half_life_days", float, 45.0),
    "importance_decay": ("recall_importance_decay", float, 0.1),
    "auto_dedupe": ("auto_dedupe", bool, True),
    "near_duplicate_ratio": ("auto_dedupe_ratio", float, 0.92),
    "embedding_enabled": ("embedding_enabled", bool, False),
    "embedding_timeout": ("embedding_timeout", float, 3.0),
    "vec_weight": ("recall_vec_weight", float, 0.45),
    "vec_min_cos": ("recall_vec_min_cos", float, 0.82),
    "vec_min_margin": ("recall_vec_min_margin", float, 0.08),
    "recall_fts_enabled": ("recall_fts_enabled", bool, False),
    "meta_query_fallback": ("meta_query_fallback", bool, True),
    "meta_query_limit": ("meta_query_limit", int, 3),
}


def settings_from_config(path=DEFAULT_CONFIG, overrides=None):
    """把插件面板配置翻译成引擎 settings（键名/类型与 utils/config.py 一致）。"""
    cfg = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    out = {}
    for key, (cfg_key, typ, default) in CONFIG_MAP.items():
        value = cfg.get(cfg_key, default)
        try:
            out[key] = typ(value)
        except (TypeError, ValueError):
            out[key] = default
    out["embedding_base"] = cfg.get("embedding_base", "http://127.0.0.1:11434")
    out["embedding_model"] = cfg.get("embedding_model", "bge-m3")
    out["diversify_ratio"] = out["near_duplicate_ratio"]
    out["identity_debug"] = False
    if overrides:
        out.update({k: v for k, v in overrides.items() if v is not None})
    return out


def copy_db(src, workdir):
    """把快照拷到一次性目录；评估里所有写（hit_count）只落在副本。"""
    if os.path.abspath(src).lower() == os.path.abspath(REAL_DB).lower():
        raise RuntimeError("拒绝在真实库上跑评估：先做只读快照")
    os.makedirs(workdir, exist_ok=True)
    dst = os.path.join(workdir, "amo_eval.db")
    shutil.copy2(src, dst)
    return dst


def freeze_now(ts):
    """把「现在」钉死到 ts，供 recall 的新鲜度/时间窗使用。返回 contextmanager。"""
    @contextlib.contextmanager
    def _cm():
        import utils.text_process as text_mod
        original = text_mod._now
        patched = []
        for name, module in list(sys.modules.items()):
            if module is None or not name:
                continue
            try:
                current = getattr(module, "_now", None)
            except Exception:
                continue
            if current is original:
                patched.append((module, current))
                module._now = lambda: int(ts)
        try:
            import utils.clock as clock_mod
            saved = clock_mod.now
            from datetime import datetime
            clock_mod.now = lambda explicit="": datetime.fromtimestamp(int(ts))
        except Exception:
            saved = None
        try:
            yield int(ts)
        finally:
            for module, current in patched:
                module._now = current
            if saved is not None:
                import utils.clock as clock_mod
                clock_mod.now = saved

    return _cm()


def render_injection(items, item_max=INJECT_ITEM_MAX, header=INJECT_HEADER):
    """把召回结果渲染成 amo 注入块的等价文本（评估「平均注入字数」用）。

    与 amo 侧一致：一条一行，带 kind 前缀；超长内容按 item_max 截断加省略号。
    """
    lines = [header]
    for item in items or []:
        content = str(item.get("content") or "").strip()
        if item_max and len(content) > item_max:
            content = content[: max(0, item_max - 1)] + "…"
        kind = str(item.get("kind") or "").strip()
        lines.append(("· [%s] %s" % (kind, content)) if kind else ("· " + content))
    return "\n".join(lines)


def ids_of(items):
    return [int(item.get("id") or 0) for item in (items or [])]


def is_relevant(item, expect_ids, expect_terms):
    """一条命中是否算相关：id 命中，或内容里出现任一期望词。"""
    if int(item.get("id") or 0) in expect_ids:
        return True
    content = str(item.get("content") or "")
    return any(term and term in content for term in expect_terms)
