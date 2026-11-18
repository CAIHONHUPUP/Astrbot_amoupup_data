# -*- coding: utf-8 -*-
import json, os, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests")); sys.path.insert(0, HERE)
import recall_common as rc
rc.install_stub()
from utils.config import PluginSettings
cfg = json.load(open(rc.DEFAULT_CONFIG, encoding="utf-8"))
ps = PluginSettings(cfg)
print("embedding_enabled", ps.get("embedding_enabled"), "base", ps.get("embedding_base"),
      "model", ps.get("embedding_model"), "timeout", ps.get("embedding_timeout"),
      "vec_weight", ps.get("vec_weight"), "vec_min_cos", ps.get("vec_min_cos"))
SNAP = r"D:\dafeiyu\backup\memory-recall-eval-20261117-105932\amo_data.snapshot.db"
tmp = tempfile.mkdtemp(prefix="amo-dbg-")
dbp = rc.copy_db(SNAP, tmp)
from models.memory_db import MemoryDB
from services.person_engine import PersonEngine
from services.memory_engine import MemoryEngine
from services.retrieval_engine import RetrievalEngine
db = MemoryDB(dbp)
persons = PersonEngine(db)
mem = MemoryEngine(db, ps, persons=persons)
ret = RetrievalEngine(db, ps, persons=persons, memory=mem)
persons.bootstrap()
t0 = time.time(); vec = mem._embed("昨天我们干了什么"); dt = time.time() - t0
print("query vec len", len(vec), "%.0fms" % (dt*1000))
rows = db.conn.execute("SELECT COUNT(*) n FROM memory_vec").fetchone()["n"]
print("memory_vec rows", rows)
ids = [r["id"] for r in db.conn.execute("SELECT id FROM memories WHERE status='active' LIMIT 5")]
print("vec for ids", {i: len(mem._load_vecs([i]).get(i, [])) for i in ids})
with rc.freeze_now(1794883956):
    items = ret.recall("大肥鱼", "ou_cec66ea605ad17f62632fb044bc5f6ac", "昨天我们干了什么", limit=6, platform="lark")
for it in items:
    print(it["id"], it.get("score"), "vec", it.get("vec_score"), str(it["content"])[:40])
db.close()
