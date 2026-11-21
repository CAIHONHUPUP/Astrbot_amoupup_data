# -*- coding: utf-8 -*-
"""按类别抽样导出真实记忆/消息，供人工设计查询语料。只读快照。"""
import sqlite3, sys, json, os

path = sys.argv[1]
conn = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

def dump(title, sql, args=()):
    print("=== " + title + " ===")
    for r in conn.execute(sql, args):
        print("%-5s %-20s imp=%.2f hits=%s %s" % (r["id"], r["kind"], r["importance"], r["hit_count"], str(r["content"])[:110].replace("\n", " ")))

dump("static/pref", "SELECT * FROM memories WHERE status='active' AND kind IN ('preference','stable_fact','core_memory','assertion','fact','habit','relation') ORDER BY id")
dump("imported newest", "SELECT * FROM memories WHERE status='active' AND kind='imported_memory' ORDER BY id DESC LIMIT 22")
dump("imported oldest", "SELECT * FROM memories WHERE status='active' AND kind='imported_memory' ORDER BY id ASC LIMIT 22")
dump("summary newest", "SELECT * FROM memories WHERE status='active' AND kind='conversation_summary' ORDER BY id DESC LIMIT 26")
dump("event oldest", "SELECT * FROM memories WHERE status='active' AND kind IN ('event','milestone','important_event','activity') ORDER BY id ASC LIMIT 26")
dump("finance", "SELECT * FROM memories WHERE status='active' AND kind LIKE 'finance%' ORDER BY id DESC LIMIT 14")
dump("user messages spread", "SELECT id, created_at, content FROM messages WHERE role='user' AND LENGTH(content)>6 ORDER BY id ASC LIMIT 40")
conn.close()
