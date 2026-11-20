# -*- coding: utf-8 -*-
"""规格反向验证：把每条规格对应的生产规则改坏，再跑那条规格，必须红。

用法（在插件根目录）：
    <venv-python> work/reverse_spec_data.py

每个用例在独立子进程里跑：子进程先 monkeypatch 生产代码，再调用
pytest -q 只跑 tests/test_spec_data_behavior.py::<对应用例>。
- 退出码 1（有用例失败）= 预期的红；
- 退出码 0（还绿）= 规格没咬住规则，脚本整体报错；
- 其它退出码 = 补丁本身炸了，同样报错。

脚本本身不修改任何生产文件，只在内存里替换函数，进程退出即恢复。
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = "tests/test_spec_data_behavior.py"


# ---------------------------------------------------------------------------
# 每个 break 只做一件事：删掉/改坏一条生产规则
# ---------------------------------------------------------------------------
def break_idem_scope():
    """A1：去重查询忽略 scope/group_id，退化成只按 (bot, uid, idem_key)。"""
    from models.dao import MemoryDao

    def broken(self, bot_id, uid, scope, group_id, idem_key):
        with self.lock:
            return self.conn.execute(
                "SELECT id, content, importance FROM memories "
                "WHERE bot_id=? AND uid=? AND idem_key=? "
                "AND COALESCE(status,'active')='active'",
                (bot_id, uid, idem_key),
            ).fetchone()

    MemoryDao.find_active_by_idem = broken


def break_recall_scope():
    """A2：召回预处理丢掉 scope/group_id 过滤，私聊召回能看见群记忆。"""
    from services.retrieval_engine import RetrievalEngine

    original = RetrievalEngine._recall_prepare

    def broken(self, bot_id, uid, query, limit, scope, group_id, platform, pid,
               half_life_days, importance_decay, pool_size, tags=""):
        return original(self, bot_id, uid, query, limit, "", "", platform, pid,
                        half_life_days, importance_decay, pool_size, tags)

    RetrievalEngine._recall_prepare = broken


def break_summary_group_key():
    """A3：同天摘要分组键去掉 scope/group_id，只按 (uid, 日期) 分组。"""
    from services import compression_rules

    def broken(uid, scope, group_id, content):
        day = compression_rules.summary_day_key(content)
        return (str(uid or ""), day) if day else None

    compression_rules.summary_group_key = broken


def break_meta_fallback_removed():
    """B1：元问题兜底整段关掉（零命中时一条都不给）。"""
    from services.retrieval_engine import RetrievalEngine

    RetrievalEngine._meta_fallback = lambda self, *args, **kwargs: []


def break_meta_ranking():
    """B2：兜底排序改成“按写入时间从旧到新”，不再看重要度+新鲜度。"""
    from services import recall_pipeline

    def broken(rows, now, half_life_days, importance_decay, limit):
        picked = []
        for row in sorted(rows, key=lambda item: int(item["created_at"] or 0)):
            item = dict(row)
            item["score"] = 0.0
            item["matched_terms"] = 0
            item["vec_score"] = 0.0
            item["meta_fallback"] = True
            picked.append(item)
            if len(picked) >= limit:
                break
        return picked

    recall_pipeline.rank_meta_fallback = broken


def break_meta_scope():
    """B3：兜底查询忽略 scope/group_id，私聊元问题会带出群记忆。"""
    from services.retrieval_engine import RetrievalEngine

    original = RetrievalEngine._meta_fallback

    def broken(self, bot_id, uid, scope, group_id, now, half_life_days, importance_decay):
        return original(self, bot_id, uid, "", "", now, half_life_days, importance_decay)

    RetrievalEngine._meta_fallback = broken


def break_meta_detection():
    """B4：元问题识别过宽（任何输入都当元问题），带实词的提问也会兜底。"""
    import services.retrieval_engine as engine

    engine._is_meta_query = lambda text: True


def break_split_noop():
    """C1：长摘要不拆了，原样返回一条。"""
    from services import compression_rules

    compression_rules.split_long_text = (
        lambda text, limit: [str(text)] if str(text) else []
    )


def break_split_lossy():
    """C2：拆分时丢掉最后一段（每条仍不超上限，但内容丢了）。"""
    from services import compression_rules

    original = compression_rules.split_long_text

    def broken(text, limit):
        pieces = original(text, limit)
        return pieces[:-1] if len(pieces) > 1 else pieces

    compression_rules.split_long_text = broken


def break_merge_unbounded():
    """C3：同天摘要把上限拿掉，永远合并（产出超长正文）。"""
    from services import compression_rules

    def broken(items, cap):
        ordered = sorted(
            items,
            key=lambda row: (len(str(row["content"] or "")), int(row["created_at"] or 0)),
            reverse=True,
        )
        keep = ordered[0]
        merged_text = str(keep["content"] or "")
        absorbed = []
        for row in ordered[1:]:
            piece = str(row["content"] or "").strip()
            if not piece or piece in merged_text:
                absorbed.append(row)
                continue
            merged_text = merged_text + "\n" + piece
            absorbed.append(row)
        return keep, merged_text, absorbed

    compression_rules.plan_summary_merge = broken


def break_merge_disabled():
    """C4：上限内的同天摘要也不合并了。"""
    from services import compression_rules

    def broken(items, cap):
        ordered = sorted(
            items,
            key=lambda row: (len(str(row["content"] or "")), int(row["created_at"] or 0)),
            reverse=True,
        )
        keep = ordered[0]
        return keep, str(keep["content"] or ""), []

    compression_rules.plan_summary_merge = broken


CASES = [
    ("A1", "SpecScopeIsolationTests::test_spec_idem_dedupe_isolated_by_scope_and_group",
     "find_active_by_idem 忽略 scope/group_id", break_idem_scope),
    ("A2", "SpecScopeIsolationTests::test_spec_recall_isolated_by_scope_and_group",
     "recall 预处理丢掉 scope/group_id 过滤", break_recall_scope),
    ("A3", "SpecScopeIsolationTests::test_spec_summary_merge_isolated_by_scope_and_group",
     "summary_group_key 忽略 scope/group_id", break_summary_group_key),
    ("B1", "SpecMetaFallbackTests::test_spec_meta_question_zero_hits_falls_back_to_2_or_3",
     "_meta_fallback 直接返回空", break_meta_fallback_removed),
    ("B2", "SpecMetaFallbackTests::test_spec_meta_fallback_ranks_by_importance_then_freshness",
     "rank_meta_fallback 改成按创建时间升序", break_meta_ranking),
    ("B3", "SpecMetaFallbackTests::test_spec_meta_fallback_respects_scope",
     "_meta_fallback 忽略 scope/group_id", break_meta_scope),
    ("B4", "SpecMetaFallbackTests::test_spec_factful_question_still_requires_relevance",
     "_is_meta_query 恒为 True", break_meta_detection),
    ("C1", "SpecLongSummaryTests::test_spec_long_summary_split_into_bounded_pieces",
     "split_long_text 不拆条", break_split_noop),
    ("C2", "SpecLongSummaryTests::test_spec_long_summary_split_is_lossless",
     "split_long_text 丢掉最后一段", break_split_lossy),
    ("C3", "SpecLongSummaryTests::test_spec_week_merge_stops_at_cap",
     "plan_summary_merge 忽略上限", break_merge_unbounded),
    ("C4", "SpecLongSummaryTests::test_spec_week_merge_within_cap_still_merges",
     "plan_summary_merge 永不合并", break_merge_disabled),
]


def _run_one(index: int) -> int:
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    try:
        import astrbot_stub

        astrbot_stub.install()
    except Exception:  # cleanup: astrbot 桩装不上不影响反向验证
        pass
    name, node, desc, mutate = CASES[index]
    print("[case] %s %s | 改坏：%s" % (name, node, desc))
    mutate()
    import pytest

    return int(pytest.main([
        SPEC + "::" + node,
        "-q",
        "-p", "no:cacheprovider",
        "--tb=line",
        "--timeout=180",
    ]))


def _run_all() -> int:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    ok = 0
    bad = []
    for index, (name, node, desc, _mutate) in enumerate(CASES):
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--only", str(index)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        failed = [line for line in lines if line.startswith("FAILED") or "AssertionError" in line]
        tail = failed[-1][:180] if failed else (lines[-1][:180] if lines else "")
        red = proc.returncode == 1
        if red:
            ok += 1
        else:
            bad.append((name, proc.returncode, tail))
        print("%s %-2s rc=%d  %s" % ("RED " if red else "BAD ", name, proc.returncode, tail))
    print("\n反向验证汇总：%d/%d 条规格在规则被改坏后变红" % (ok, len(CASES)))
    if bad:
        for name, code, tail in bad:
            print("  未变红：%s rc=%s %s" % (name, code, tail))
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--only":
        raise SystemExit(_run_one(int(sys.argv[2])))
    raise SystemExit(_run_all())
