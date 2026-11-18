# -*- coding: utf-8 -*-
"""复盘未命中查询：pid、候选数、目标行是否进池、闸门为什么挡掉。"""
import json, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests")); sys.path.insert(0, HERE)
import recall_common as rc
rc.install_stub()
from utils.config import PluginSettings
from services import recall_pipeline
from utils.text_process import _clean, _informative_terms, _query_chars
from models.memory_db import MemoryDB
from services.person_engine import PersonEngine
from services.memory_engine import MemoryEngine
from services.retrieval_engine import RetrievalEngine

cfg = json.load(open(rc.DEFAULT_CONFIG, encoding="utf-8"))
ps = PluginSettings(cfg)
SNAP = r"D:\dafeiyu\backup\memory-recall-eval-20261117-105932\amo_data.snapshot.db"
BOT = "大肥鱼"
UID = "ou_cec66ea605ad17f62632fb044bc5f6ac"
NOW = 1794883956
CASES = [
    ("q06", "我让你记住的那两个字是什么", [751, 790]),
    ("q18", "迟到罚了多少钱", [1138, 1110]),
    ("q10", "11月6号晚上我吃了什么", [900, 894, 891]),
    ("q32", "我喜欢吃哪种面", [973, 976, 978, 981]),
    ("q40", "最近三天我都干嘛了", [1144, 1147, 1135]),
    ("q44", "我这个月花了多少钱", [1118, 1136, 1110]),
]

tmp = tempfile.mkdtemp(prefix="amo-dbg2-")
print("pid_for_uid", "?")
for qid, query, targets in CASES:
    dbp = rc.copy_db(SNAP, os.path.join(tmp, qid))
    db = MemoryDB(dbp)
    persons = PersonEngine(db)
    mem = MemoryEngine(db, ps, persons=persons)
    ret = RetrievalEngine(db, ps, persons=persons, memory=mem)
    persons.bootstrap()
    pid_u = persons.pid_for_uid(BOT, UID)
    pid_plat: object
    try:
        pid_plat = persons.person_pid(BOT, "lark", UID)
    except Exception as exc:
        pid_plat = "ERR %r" % exc
    with rc.freeze_now(NOW):
        ctx = ret._recall_prepare(BOT, UID, query, 6, "private", "", "lark", 0, None, None, None)
        rows = ret._recall_candidates(ctx)
        sp = ret._recall_scores(ctx, rows)
        scored = ret._recall_score(ctx, rows, sp)
    ids = [int(r["id"]) for r in rows]
    print("\n== %s %s" % (qid, query))
    print("pid_uid=%s pid_platform=%s ctx.pid=%s text=%r" % (pid_u, pid_plat, ctx["pid"], ctx["text"]))
    print("candidates=%d targets_in_pool=%s" % (len(rows), [t for t in targets if t in ids]))
    print("infos=%s" % _informative_terms(ctx["text"])[:12])
    print("qchars=%s" % _query_chars(ctx["text"]))
    print("scored=%d" % len(scored))
    for t in targets:
        row = next((r for r in rows if int(r["id"]) == t), None)
        if row is None:
            print("  target %s: NOT IN POOL" % t)
            continue
        idx = rows.index(row)
        doc_set = set(sp["docs"][idx])
        print("  target %s: raw=%.4f strong=%d char=%d infos_hit=%s content=%s" % (
            t, sp["raw"][idx], len(set(sp["infos"]) & doc_set), len(set(sp["qchars"]) & doc_set),
            sorted(set(sp["infos"]) & doc_set)[:6], str(row["content"])[:50]))
        item = recall_pipeline.score_row(row, idx, sp, ctx, max(sp["raw"]) if sp["raw"] else 0.0, len(sp["infos"]) or 1)
        print("     score_row -> %s" % ("None(被闸挡)" if item is None else "score=%.4f" % item["score"]))
    db.close()
