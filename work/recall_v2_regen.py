# -*- coding: utf-8 -*-
"""重新生成 tests/contracts/recall_golden.json（召回行为有意变更后跑一次）。

同时打印 score_rows 的三个样例分数，方便核对「新鲜度改用 created_at」的影响。
用法：python work/recall_v2_regen.py [--write]
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from fixtures import FIXED_NOW  # noqa: E402
from support import recall_corpus  # noqa: E402
from services import recall_pipeline  # noqa: E402
from utils.text_process import (  # noqa: E402
    _bm25_scores, _informative_terms, _query_chars, _recall_tokens,
)

GOLDEN = os.path.join(ROOT, "tests", "contracts", "recall_golden.json")


def _row(rid, content, importance=0.5, created_days=0, hit_days=0, kind="fact"):
    return {
        "id": rid, "content": content, "kind": kind, "importance": importance,
        "created_at": FIXED_NOW - int(created_days * 86400),
        "last_hit_at": FIXED_NOW - int(hit_days * 86400),
        "hit_count": 0, "scope": "private", "group_id": "", "subject": "user",
    }


def debug_scores():
    query = "螺蛳粉"
    rows = [
        _row(1, "主人喜欢吃螺蛳粉，尤其是加辣的", importance=0.8, created_days=60, hit_days=1),
        _row(2, "今天天气不错", importance=0.3, created_days=10, hit_days=1),
        _row(3, "螺蛳粉是柳州的小吃", importance=0.5, created_days=5, hit_days=2),
    ]
    contents = [str(row["content"] or "") for row in rows]
    q_tokens = _recall_tokens(query)
    docs = [_recall_tokens(c) for c in contents]
    features = {
        "contents": contents, "q_tokens": q_tokens, "docs": docs,
        "raw": _bm25_scores(q_tokens, docs), "q_vec": [], "vec_map": {},
        "vec_weight": 0.45, "min_terms": 1,
        "infos": set(_informative_terms(query)), "qchars": set(_query_chars(query)),
        "coss": {}, "best_cos": 0.0, "vec_pass": False, "vec_pass_ids": set(),
    }
    ctx = {"text": query, "span": None, "now": FIXED_NOW,
           "half_life_days": 45.0, "importance_decay": 0.1}
    for item in recall_pipeline.score_rows(rows, features, ctx):
        print("score id=%s %.4f (rel? imp=%s created_days)" % (
            item["id"], item["score"], item.get("importance")))


def regen(write=False):
    tmp = tempfile.mkdtemp(prefix="recall-golden-")
    try:
        data = recall_corpus.capture(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    body = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)
    print("golden keys:", list(data)[:8], "queries:", len(data))
    if write:
        with open(GOLDEN, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        print("wrote", GOLDEN, len(body), "bytes")
    else:
        print("dry-run; pass --write to update")


if __name__ == "__main__":
    debug_scores()
    regen("--write" in sys.argv)
