# -*- coding: utf-8 -*-
import os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import astrbot_stub; astrbot_stub.install()
from support.store_fixture import DataStore

BOT, UID = "bot1", "302578963"
tmp = tempfile.mkdtemp()
store = DataStore(os.path.join(tmp, "d.db"), settings={
    "embedding_enabled": False, "embedding_backfill": False, "recall_recent_pool": 2})
cat = store.remember(BOT, UID, "主人养了一只橘色的猫，叫团子", importance=0.6)["memory_id"]
for i in range(12):
    store.remember(BOT, UID, "2026-11-%02d 流水账 %d：今天吃了第 %d 顿饭" % (i % 5 + 1, i, i), importance=0.3)
now = int(time.time())
with store.lock:
    store.conn.execute("UPDATE memories SET created_at=?, last_hit_at=? WHERE id=?", (now - 5000, now - 5000, cat))
    others = store.conn.execute("SELECT id FROM memories WHERE id<>? ORDER BY id", (cat,)).fetchall()
    for off, row in enumerate(others):
        store.conn.execute("UPDATE memories SET created_at=?, last_hit_at=? WHERE id=?", (now - 100 + off, now - 100 + off, int(row["id"])))
    store.conn.commit()
store.settings["recall_recent_pool"] = 2
ret = store.retrieval
for q in ("你还记得团子吗", "毛孩子叫啥来的"):
    ctx = ret._recall_prepare(BOT, UID, q, 6, "private", "", "", 0, None, None, None)
    print("Q:", q, "| meta=", ctx["meta_query"], "| recent_pool=", ret.setting("recall_recent_pool"))
    rows = ret._recall_candidates(ctx)
    print("  candidates:", [int(r["id"]) for r in rows])
    out = store.recall(BOT, UID, q)
    print("  recall:", [(int(r["id"]), r.get("score"), r.get("meta_fallback", False)) for r in out])
store.close()
