# -*- coding: utf-8 -*-
import os, sys, tempfile, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import astrbot_stub; astrbot_stub.install()
from support.store_fixture import DataStore
from services import graph_recall

BOT, UID, COUSIN = "bot1", "302578963", "cousin1"
tmp = tempfile.mkdtemp(); store = DataStore(os.path.join(tmp, "d.db"), settings={"embedding_enabled": False})
now = int(time.time())
with store.lock:
    c = store.conn
    c.execute("INSERT OR REPLACE INTO persons(pid,bot_id,name,created_at,updated_at) VALUES(?,?,?,?,?)", (2, BOT, "小周", now, now))
    c.execute("INSERT OR REPLACE INTO person_links(bot_id,platform,uid,pid,name,created_at) VALUES(?,?,?,?,?,?)", (BOT, "aiocqhttp", COUSIN, 2, "小周", now))
    c.execute("INSERT OR REPLACE INTO relations(bot_id,a_uid,b_uid,relation,note,updated_at) VALUES(?,?,?,?,?,?)", (BOT, UID, COUSIN, "表哥", "", now))
    c.execute("INSERT INTO memories(bot_id,uid,pid,platform,scope,group_id,kind,subject,content,importance,created_at,last_hit_at,status,tags,hit_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
              (BOT, UID, 2, "aiocqhttp", "private", "", "fact", "other", "小周最近转了行，在做建筑设计", 0.7, now, now, "active", ""))
    c.commit()
ctx = store.retrieval._recall_prepare(BOT, UID, "我表哥最近怎么样", 6, "private", "", "", 0, None, None, None)
print("ctx uid=", repr(ctx["uid"]), "pid=", ctx["pid"], "bot=", ctx["bot_id"])
print("resolve:", graph_recall.resolve_entities(store.conn, store.lock, BOT, ctx["uid"], ctx["text"]))
print("links:", [dict(r) for r in store.conn.execute("SELECT uid,pid,name FROM person_links").fetchall()])
print("rels:", [dict(r) for r in store.conn.execute("SELECT a_uid,b_uid,relation FROM relations").fetchall()])
store.close()
