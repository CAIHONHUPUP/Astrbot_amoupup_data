# -*- coding: utf-8 -*-
"""冒烟：在快照副本上跑现有 recall。"""
import json, os, shutil, sqlite3, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))

import recall_common as rc
rc.install_stub()
from support.store_fixture import DataStore

SNAP = r"D:\dafeiyu\backup\memory-recall-eval-20261117-105932\amo_data.snapshot.db"
cfg = json.load(open(rc.DEFAULT_CONFIG, encoding="utf-8"))
BOT = cfg.get("bot_id") or "default"

tmp = tempfile.mkdtemp(prefix="amo-smoke-")
dbp = rc.copy_db(SNAP, tmp)
store = DataStore(dbp, settings=rc.settings_from_config())
UID = "ou_cec66ea605ad17f62632fb044bc5f6ac"
print("bot", BOT, "pid", store.persons.pid_for_uid(BOT, UID),
      "canon", store.persons.canonical_uid_for_uid(BOT, UID))

queries = ["老婆", "我发烧了", "昨天我们干了什么", "你还记得我吗", "火锅", "量子力学", "自拍", "存款定期", "老公心疼我", "上次说的那个小挂件呢"]
now = 1794884361
for q in queries:
    t0 = time.time()
    with rc.freeze_now(now):
        items = store.recall(BOT, UID, q, limit=6)
    dt = (time.time() - t0) * 1000
    print("---", q, "n=%d" % len(items), "%.0fms" % dt)
    for it in items:
        print("   ", it["id"], it.get("kind"), it.get("score"), it.get("matched_terms"),
              str(it["content"])[:36].replace("\n", " "))
store.close()
shutil.rmtree(tmp, ignore_errors=True)
print("DONE")
