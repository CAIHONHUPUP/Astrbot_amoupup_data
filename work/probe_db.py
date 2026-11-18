# -*- coding: utf-8 -*-
"""只读探查真实库：结构、规模、近样本。绝不写真实库。"""
import sqlite3, sys, os, json, time

SRC = r"D:\dafeiyu\astrbot\data\plugin_data\Astrbot_amoupup_data\amo_data.db"

def ro(path):
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=8)
    conn.row_factory = sqlite3.Row
    return conn

conn = ro(SRC)
print("size", os.path.getsize(SRC))
print("journal", conn.execute("PRAGMA journal_mode").fetchone()[0])
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("tables", tables)
for t in tables:
    try:
        n = conn.execute("SELECT COUNT(*) FROM [%s]" % t).fetchone()[0]
    except Exception as e:
        n = "ERR:%r" % (e,)
    print("count", t, n)

for t in ("memories", "messages", "persons", "profiles"):
    if t in tables:
        cols = [r[1] + ":" + r[2] for r in conn.execute("PRAGMA table_info([%s])" % t)]
        print("cols", t, cols)

if "memories" in tables:
    print("kinds", [tuple(r) for r in conn.execute("SELECT kind, COUNT(*) FROM memories GROUP BY kind ORDER BY 2 DESC")])
    print("status", [tuple(r) for r in conn.execute("SELECT status, COUNT(*) FROM memories GROUP BY status")])
    print("bots", [tuple(r) for r in conn.execute("SELECT bot_id, COUNT(*) FROM memories GROUP BY bot_id")])
    print("uids_top", [tuple(r) for r in conn.execute("SELECT uid, COUNT(*) FROM memories GROUP BY uid ORDER BY 2 DESC LIMIT 10")])
    row = conn.execute("SELECT MIN(created_at), MAX(created_at), MIN(id), MAX(id) FROM memories").fetchone()
    print("time_range", tuple(row), "now", int(time.time()))
    print("scopes", [tuple(r) for r in conn.execute("SELECT scope, COUNT(*) FROM memories GROUP BY scope")])
if "messages" in tables:
    row = conn.execute("SELECT MIN(created_at), MAX(created_at), COUNT(*) FROM messages").fetchone()
    print("msg_range", tuple(row))
    print("msg_umos", [tuple(r) for r in conn.execute("SELECT umo, COUNT(*) FROM messages GROUP BY umo ORDER BY 2 DESC LIMIT 8")])
conn.close()
print("OK")
