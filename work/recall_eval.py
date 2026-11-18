# -*- coding: utf-8 -*-
"""真实回放召回评估（可复跑）。

对真实库的只读快照逐条查询跑现有 recall 管线，对比基线，产出
precision@k / 命中率 / 空召回率 / 平均注入字数 / 近重复注入率。

用法：
    python work/recall_eval.py                        # 全量 + 全部 arm
    python work/recall_eval.py --arms like,kw,current
    python work/recall_eval.py --arms current,fixed --json work/report_fixed.json
    python work/recall_eval.py --ids q34,q45 --arms current

安全：只读快照 -> 每条查询新建一次性副本；拒绝在真实库路径上运行。

arm 说明：
    like     朴素子串基线：content LIKE '%query%'，按最近命中排序（没有打分）
    kw       现策略但关语义：BM25 + 重要度 + 新鲜度 + 两道闸 + 注入去重
    before   修复前的生产行为（旧证据词构造 + diversify 0.75）
    current  修复后的生产行为（语义开，bge-m3）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time

from typing import Any, Dict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))
sys.path.insert(0, HERE)

import recall_common as rc  # noqa: E402

rc.install_stub()

from services.memory_engine import MemoryEngine  # noqa: E402
from services.person_engine import PersonEngine  # noqa: E402
from services.retrieval_engine import RetrievalEngine  # noqa: E402
from models.memory_db import MemoryDB  # noqa: E402
from utils.config import PluginSettings  # noqa: E402
from utils.text_process import (  # noqa: E402
    STOP_BIGRAMS, STOP_CHARS, _recall_tokens,
)

#: 流水账/机器写的东西：元问题兜底里出现它们就是噪声
LOG_KINDS = {
    "social_news", "bot_media", "proactive_message", "tool_memory", "self_action",
    "finance", "finance_long", "finance_long_income",
    "finance_short_pnl", "finance_short_fund_outage", "finance_decision",
    "finance_permanent",
}
#: 同一注入块里两条记忆的二元组 Jaccard 超过它算「近重复一起注入」
DUP_JACCARD = 0.6


class SettingsView:
    """把 PluginSettings（真实映射）叠一层覆盖，供引擎的 .get 使用。"""

    def __init__(self, config, overrides=None):
        self.inner = PluginSettings(config)
        self.overrides = dict(overrides or {})

    def get(self, key, default=None):
        if key in self.overrides:
            return self.overrides[key]
        return self.inner.get(key, default)


def build_engine(db_path, settings):
    db = MemoryDB(db_path, settings={"recall_fts_enabled": bool(settings.get("recall_fts_enabled", False))})
    persons = PersonEngine(db)
    memory = MemoryEngine(db, settings, persons=persons)
    retrieval = RetrievalEngine(db, settings, persons=persons, memory=memory)
    persons.bootstrap()
    return db, persons, retrieval


def pid_dist(path):
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT COALESCE(pid,0), COUNT(*) FROM memories GROUP BY 1 ORDER BY 1"
        ).fetchall()
    finally:
        conn.close()
    return {int(r[0]): int(r[1]) for r in rows}


def naive_like(copy_path, bot_id, uid, pid, query, k):
    """基线 B0：content LIKE '%query%'，命中后按最近命中排序；没有打分。"""
    conn = sqlite3.connect(copy_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = "id, content, kind, subject, importance, created_at, last_hit_at, hit_count, scope, group_id"
        base = "SELECT %s FROM memories WHERE bot_id=? AND COALESCE(status,'active')='active'" % cols
        params = [bot_id]
        if pid:
            base += " AND (pid=? OR (COALESCE(pid,0)=0 AND uid=?))"
            params.extend([int(pid), uid])
        else:
            base += " AND uid=?"
            params.append(uid)
        rows = conn.execute(
            base + " AND content LIKE ? ORDER BY last_hit_at DESC, created_at DESC LIMIT ?",
            params + ["%" + str(query) + "%", int(k)],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 修复 A：证据词构造。原实现先删虚词再配对，把「迟到」拆成 迟|到 -> 无 bigram，
# 「你记|住」这类真词也一并丢掉，导致精确命中被关键词闸挡掉。
# 修法：按原始字序列配对（保邻接），只丢弃「两个字都是虚词」的二元组。
# ---------------------------------------------------------------------------

#: 纯连接词/代词：和它们组成的二元组不是「词」（「我想」「的新」「听吗」），
#: 不能当命中证据；而「迟到」「记住」「个月」里的虚词是构词的一部分，必须保留。
GLUE_CHARS = set("的了着过吗呢吧啊呀哦嗯嘛么什怎谁哪几我你他她它咱您")


def informative_terms_fixed(text):
    s = str(text or "").lower()
    words = [w for w in re.findall(r"[a-z0-9_]{2,}", s) if w not in STOP_CHARS]
    chars = re.findall(r"[\u4e00-\u9fff]", s)
    if not chars:
        return words
    if len(chars) == 1:
        return words + chars
    pairs = []
    for a, b in zip(chars, chars[1:]):
        if a in STOP_CHARS and b in STOP_CHARS:
            continue
        if a in GLUE_CHARS or b in GLUE_CHARS:
            continue
        pairs.append(a + b)
    return words + [p for p in pairs if p not in STOP_BIGRAMS]


def legacy_informative_terms(text):
    """修复前的实现（先删虚词再配对），用于可复跑的 before 对照。"""
    s = str(text or "").lower()
    words = [w for w in re.findall(r"[a-z0-9_]{2,}", s) if w not in STOP_CHARS]
    chars = [c for c in re.findall(r"[\u4e00-\u9fff]", s) if c not in STOP_CHARS]
    if not chars:
        return words
    if len(chars) == 1:
        return words + chars
    return words + [a + b for a, b in zip(chars, chars[1:])
                    if (a + b) not in STOP_BIGRAMS]


def apply_evidence_fix():
    """把修复实现同时装到 text_process 与已经按值 import 的 retrieval_engine。"""
    import utils.text_process as text_mod
    import services.retrieval_engine as ret_mod
    text_mod._informative_terms = informative_terms_fixed
    ret_mod._informative_terms = informative_terms_fixed


def apply_legacy():
    """把修复前实现装回去（before 对照臂）。"""
    import utils.text_process as text_mod
    import services.retrieval_engine as ret_mod
    text_mod._informative_terms = legacy_informative_terms
    ret_mod._informative_terms = legacy_informative_terms


def useful_item(item):
    """元问题语义下「值得注入」：不是流水账/财务 JSON。"""
    kind = str(item.get("kind") or "")
    content = str(item.get("content") or "").strip()
    if kind.startswith("finance") or kind in LOG_KINDS:
        return False
    if content.startswith("{"):
        return False
    return True


def grade(query, items, k):
    """给一条查询的召回结果打分。"""
    expect_ids = set(int(i) for i in (query.get("expect_ids") or []))
    terms = [t for t in (query.get("expect_terms") or []) if t]
    grader = query.get("grader") or "terms"
    if grader == "abstain":
        flags = [False] * len(items)
        answerable = False
    elif grader == "useful":
        flags = [useful_item(it) for it in items]
        answerable = True
    else:
        flags = [rc.is_relevant(it, expect_ids, terms) for it in items]
        answerable = True
    hits = sum(1 for f in flags if f)
    first = next((idx + 1 for idx, f in enumerate(flags) if f), 0)
    # 近重复注入：同一块里两条记忆的二元组 Jaccard >= DUP_JACCARD
    toks = [set(_recall_tokens(str(it.get("content") or ""))) for it in items]
    dup = set()
    for i in range(len(toks)):
        for j in range(i + 1, len(toks)):
            a, b = toks[i], toks[j]
            if not a or not b:
                continue
            if len(a & b) / float(len(a | b)) >= DUP_JACCARD:
                dup.add(i)
                dup.add(j)
    raw_block = rc.render_injection(items, item_max=0)
    capped_block = rc.render_injection(items)
    return {
        "id": query["id"], "cat": query["cat"], "query": query["query"], "grader": grader,
        "n": len(items), "relevant": hits, "precision_at_k": hits / float(k),
        "precision_returned": (hits / float(len(items))) if items else 0.0,
        "hit": bool(hits), "mrr": (1.0 / first) if first else 0.0,
        "answerable": answerable, "abstained": len(items) == 0,
        "dup_items": len(dup), "dup_rate": (len(dup) / float(len(items))) if items else 0.0,
        "inject_chars": len(raw_block), "inject_chars_capped": len(capped_block),
        "content_chars": sum(len(str(it.get("content") or "")) for it in items),
        "vec_used": any(float(it.get("vec_score") or 0) > 0 for it in items),
        "ids": [int(it.get("id") or 0) for it in items],
        "kinds": [str(it.get("kind") or "") for it in items],
        "scores": [it.get("score") for it in items],
        "matched": [it.get("matched_terms") for it in items],
        "vecs": [it.get("vec_score") for it in items],
        "preview": [str(it.get("content") or "")[:70] for it in items],
    }


def summarize(rows, k):
    ans = [r for r in rows if r["answerable"]]
    unans = [r for r in rows if not r["answerable"]]
    def mean(values):
        values = list(values)
        return (sum(values) / float(len(values))) if values else 0.0
    return {
        "queries": len(rows),
        "hit_rate": mean(1.0 if r["hit"] else 0.0 for r in ans),
        "precision_at_k": mean(r["precision_at_k"] for r in rows),
        "precision_at_k_answerable": mean(r["precision_at_k"] for r in ans),
        "precision_returned": mean(r["precision_returned"] for r in rows),
        "mrr": mean(r["mrr"] for r in ans),
        "empty_rate": mean(1.0 if r["abstained"] else 0.0 for r in rows),
        "empty_rate_answerable": mean(1.0 if r["abstained"] else 0.0 for r in ans),
        "abstain_accuracy": mean(1.0 if r["abstained"] else 0.0 for r in unans),
        "noise_rate_unanswerable": mean(1.0 if r["n"] else 0.0 for r in unans),
        "avg_items": mean(r["n"] for r in rows),
        "avg_inject_chars": mean(r["inject_chars"] for r in rows),
        "avg_inject_chars_capped": mean(r["inject_chars_capped"] for r in rows),
        "avg_content_chars": mean(r["content_chars"] for r in rows),
        "dup_inject_rate": mean(r["dup_rate"] for r in rows),
        "vec_used_rate": mean(1.0 if r["vec_used"] else 0.0 for r in rows),
        "avg_ms": mean(r.get("ms", 0) for r in rows),
    }


def by_group(rows, key, k):
    groups = {}
    for row in rows:
        groups.setdefault(row[key], []).append(row)
    return {name: summarize(items, k) for name, items in sorted(groups.items())}


#: 修复臂的设置覆盖
FIXED_OVERRIDES: Dict[str, Any] = {
    # 注入去重（diversify_ratio）在生产里走默认 0.75；这里保持不动，
    # 只修证据词，避免一次改两个变量说不清归因。
}


#: 尾部截断：分数低于 top*ratio 的尾巴不再注入（至少留 min_keep 条）
TAIL_RATIO = 0.5
TAIL_MIN_KEEP = 2


def tail_cut(items, ratio=TAIL_RATIO, min_keep=TAIL_MIN_KEEP):
    if len(items) <= min_keep:
        return list(items)
    try:
        top = max(float(it.get("score") or 0.0) for it in items)
    except (TypeError, ValueError):
        return list(items)
    if top <= 0:
        return list(items)
    kept = [it for it in items
            if float(it.get("score") or 0.0) >= top * float(ratio)]
    if len(kept) < min_keep:
        kept = list(items[:min_keep])
    return kept


def arm_settings(name):
    if name == "kw":
        return {"embedding_enabled": False}
    if name == "like":
        return {}
    if name == "current":
        return {}
    if name in ("fixed", "fixed2"):
        return dict(FIXED_OVERRIDES)
    if name == "before":
        return {"diversify_ratio": 0.75}
    if name == "prepp":
        # 2026-11-18 升级前的行为：无数向量候选、无拆词 OR、无实体路、strict 语义闸
        return {"recall_vector_candidates": 0, "recall_token_or": False,
                "recall_graph_route": False, "recall_vec_gate": "strict"}
    raise SystemExit("unknown arm: " + name)


def run(args):
    with open(args.corpus, encoding="utf-8") as fh:
        corpus = json.load(fh)
    queries = corpus["queries"]
    if args.ids:
        wanted = {q.strip() for q in args.ids.split(",") if q.strip()}
        queries = [q for q in queries if q["id"] in wanted]
    with open(args.config, encoding="utf-8-sig") as fh:
        config = json.load(fh)
    bot_id = config.get("bot_id") or "default"
    uid = args.uid
    k = args.k

    conn = sqlite3.connect("file:" + args.snapshot.replace("\\", "/") + "?mode=ro", uri=True)
    try:
        now = int(conn.execute("SELECT MAX(created_at) FROM messages").fetchone()[0]) + 60
        before = pid_dist(args.snapshot)
    finally:
        conn.close()
    if args.now:
        now = int(args.now)

    arms = [a for a in args.arms.split(",") if a]
    tmp_root = tempfile.mkdtemp(prefix="amo-recall-eval-")
    report = {
        "meta": {
            "snapshot": args.snapshot, "config": args.config, "corpus": args.corpus,
            "bot_id": bot_id, "uid": uid, "k": k, "now": now,
            "pid_dist_snapshot": before, "arms": arms, "queries": len(queries),
        },
        "arms": {},
    }
    t_start = time.time()
    global_overrides = {}
    for item in (args.override or "").split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        try:
            global_overrides[key] = float(value) if "." in value else int(value)
        except ValueError:
            global_overrides[key] = value
    for arm in arms:
        overrides = dict(arm_settings(arm))
        overrides.update(global_overrides)
        if args.diversify is not None and arm != "like":
            overrides["diversify_ratio"] = float(args.diversify)
        settings = SettingsView(config, overrides)
        rows = []
        pid_after = None
        for q in queries:
            cell = os.path.join(tmp_root, arm + "-" + q["id"])
            copy_path = rc.copy_db(args.snapshot, cell)
            db, persons, retrieval = build_engine(copy_path, settings)
            if pid_after is None:
                pid_after = pid_dist(copy_path)
            pid = int(persons.pid_for_uid(bot_id, uid) or 0)
            t0 = time.time()
            if arm == "like":
                items = naive_like(copy_path, bot_id, uid, pid, q["query"], k)
            else:
                if arm in ("fixed", "fixed2"):
                    apply_evidence_fix()
                elif arm == "before":
                    apply_legacy()
                else:
                    restore_evidence()
                with rc.freeze_now(now):
                    items = retrieval.recall(bot_id, uid, q["query"], limit=k,
                                             scope="private", platform=args.platform)
                if arm == "fixed2":
                    items = tail_cut(items)
            elapsed = (time.time() - t0) * 1000.0
            row = grade(q, items, k)
            row["ms"] = round(elapsed, 1)
            rows.append(row)
            db.close()
            shutil.rmtree(cell, ignore_errors=True)
        summary = summarize(rows, k)
        report["arms"][arm] = {
            "settings_overrides": overrides,
            "pid_dist_after_init": pid_after,
            "metrics": summary,
            "by_cat": by_group(rows, "cat", k),
            "queries": rows,
        }
        print("[arm %-8s] hit=%.2f p@k=%.3f p(ret)=%.3f empty=%.2f abstain=%.2f inject=%.0f字 dup=%.3f %.0fms" % (
            arm, summary["hit_rate"], summary["precision_at_k"], summary["precision_returned"],
            summary["empty_rate"], summary["abstain_accuracy"],
            summary["avg_inject_chars"], summary["dup_inject_rate"], summary["avg_ms"]))
    report["meta"]["elapsed_s"] = round(time.time() - t_start, 1)
    shutil.rmtree(tmp_root, ignore_errors=True)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print("report -> " + args.json)
    return report


#: 原实现（arm 之间来回切，保证 current 不受 fixed 污染）
def restore_evidence():
    import utils.text_process as text_mod
    import services.retrieval_engine as ret_mod
    original = ORIGINAL_INFORMATIVE
    text_mod._informative_terms = original
    ret_mod._informative_terms = original


import utils.text_process as _tp  # noqa: E402
ORIGINAL_INFORMATIVE = _tp._informative_terms


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=os.environ.get("AMO_EVAL_SNAPSHOT", ""))
    ap.add_argument("--corpus", default=os.path.join(HERE, "corpus_real.json"))
    ap.add_argument("--config", default=rc.DEFAULT_CONFIG)
    ap.add_argument("--uid", default="ou_cec66ea605ad17f62632fb044bc5f6ac")
    ap.add_argument("--platform", default="lark")
    ap.add_argument("--k", type=int, default=6)
    ap.add_argument("--now", type=int, default=0)
    ap.add_argument("--arms", default="like,kw,before,current")
    ap.add_argument("--ids", default="")
    ap.add_argument("--diversify", type=float, default=None,
                    help="覆盖注入去重阈值（0=关；生产默认 0.75）")
    ap.add_argument("--override", default="", help="全局设置覆盖，k=v,k=v（如 recall_vec_gate_floor=0.70）")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    if not args.snapshot:
        raise SystemExit("需要 --snapshot（真实库的只读快照），见 work/snapshot.py")
    return run(args)


if __name__ == "__main__":
    main()
