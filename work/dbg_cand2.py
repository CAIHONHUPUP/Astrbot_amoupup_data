# -*- coding: utf-8 -*-
import os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import astrbot_stub; astrbot_stub.install()
from support.store_fixture import DataStore
from utils.text_process import _informative_terms

BOT, UID = "bot1", "302578963"
tmp = tempfile.mkdtemp()
store = DataStore(os.path.join(tmp, "d.db"), settings={"embedding_enabled": False, "recall_backfill": False, "recall_recent_pool": 2})
cat = store.remember(BOT, UID, "主人养了一只橘色的猫，叫团子", importance=0.6)["memory_id"]
for i in range(3):
    store.remember(BOT, UID, "流水账 %d 今天吃了第 %d 顿饭" % (i, i), importance=0.3)
rows = store.conn.execute("SELECT id, created_at, last_hit_at FROM memories ORDER BY id").fetchall()
print("now=", int(time.time()))
print("rows before:", [(int(r["id"]), int(r["created_at"]), int(r["last_hit_at"])) for r in rows])
now = int(time.time())
with store.lock:
    store.conn.execute("UPDATE memories SET created_at=?, last_hit_at=? WHERE id=?", (now - 5000, now - 5000, cat))
    store.conn.commit()
rows = store.conn.execute("SELECT id, created_at, last_hit_at FROM memories ORDER BY id").fetchall()
print("rows after:", [(int(r["id"]), int(r["created_at"]), int(r["last_hit_at"])) for r in rows])
q = "你还记得团子吗"
print("terms:", _informative_terms(q))
ctx = store.retrieval._recall_prepare(BOT, UID, q, 6, "private", "", "", 0, None, None, None)
print("token:", [int(r["id"]) for r in store.retrieval._token_candidates(ctx["base"], ctx["params"], q, 400)])
print("recent limit:", __import__("services.recall_pipeline", fromlist=["x"]).clamp_recent_pool(2, 6))
store.close()
