# -*- coding: utf-8 -*-
"""从快照里抽样本，供设计查询语料。只读。"""
import sqlite3, sys, json, os, time

path = sys.argv[1]
conn = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
now = int(time.time())

def rows(sql, args=()):
    return [dict(r) for r in conn.execute(sql, args)]

print("=== persons ===")
for r in rows("SELECT * FROM persons"):
    print(json.dumps(r, ensure_ascii=False))
print("=== profiles ===")
for r in rows("SELECT * FROM profiles"):
    print(json.dumps(r, ensure_ascii=False))

print("=== memories recent 45 (active) ===")
for r in rows("SELECT id,uid,pid,platform,kind,content,importance,hit_count,created_at,last_hit_at,tags,subject FROM memories WHERE status='active' ORDER BY created_at DESC LIMIT 45"):
    r["age_days"] = round((now - r["created_at"]) / 86400.0, 1)
    print(json.dumps(r, ensure_ascii=False))

print("=== memories by kind: preference/event/milestone/daily_digest ===")
for r in rows("SELECT id,kind,content,importance,created_at,status FROM memories WHERE kind IN ('preference','event','milestone','important_event','daily_digest','stable_fact','fact','core_memory','conversation_summary') AND status='active' ORDER BY created_at DESC LIMIT 40"):
    print(json.dumps(r, ensure_ascii=False))

print("=== user messages recent 30 ===")
for r in rows("SELECT id,umo,uid,name,role,content,created_at FROM messages WHERE role='user' ORDER BY created_at DESC LIMIT 30"):
    print(json.dumps(r, ensure_ascii=False))
conn.close()
