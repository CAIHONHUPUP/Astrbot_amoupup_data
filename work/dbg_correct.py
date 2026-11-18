# -*- coding: utf-8 -*-
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import astrbot_stub; astrbot_stub.install()
from support.store_fixture import DataStore
BOT, UID = "bot1", "302578963"
tmp = tempfile.mkdtemp(); store = DataStore(os.path.join(tmp, "d.db"), settings={"embedding_enabled": False, "auto_dedupe": False})
old = store.remember(BOT, UID, "主人喜欢吃螺蛳粉", kind="preference", importance=0.6)
print("old:", old)
res = store.memory.correct(BOT, UID, "螺蛳粉", "主人不吃螺蛳粉，闻到味就躲")
print("res:", res)
for r in store.conn.execute("SELECT id,uid,status,superseded_by,substr(content,1,24) c FROM memories").fetchall():
    print(dict(r))
print("like:", store.dao.find_active_like(BOT, UID, "螺蛳粉", 0, 5))
store.close()
