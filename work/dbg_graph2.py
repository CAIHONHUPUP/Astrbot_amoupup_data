# -*- coding: utf-8 -*-
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import astrbot_stub; astrbot_stub.install()
from support.store_fixture import DataStore
from services import entity_graph
BOT, UID = "bot1", "302578963"
tmp = tempfile.mkdtemp()
store = DataStore(os.path.join(tmp, "d.db"), settings={"embedding_enabled": False, "auto_dedupe": False})
a = store.remember(BOT, UID, "小周送了我一条「围巾」")["memory_id"]
b = store.remember(BOT, UID, "小周最近换了工作")["memory_id"]
print("a,b", a, b)
print("entities", [dict(r) for r in store.conn.execute("SELECT id,name FROM entities").fetchall()])
print("mem_ent", [dict(r) for r in store.conn.execute("SELECT memory_id,entity_id FROM memory_entities").fetchall()])
print("edges", [dict(r) for r in store.conn.execute("SELECT a_id,b_id,weight FROM entity_edges").fetchall()])
print("extract q", entity_graph.extract_entities("围巾"))
print("ids", entity_graph.memories_for_query(store.conn, store.lock, BOT, "围巾", 10))
print("recall", [(r["id"], r["content"]) for r in store.recall(BOT, UID, "围巾")])
store.close()
