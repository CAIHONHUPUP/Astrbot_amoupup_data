# -*- coding: utf-8 -*-
import json, sqlite3
p = r"D:\dafeiyu\astrbot\data\plugin_data\Astrbot_amoupup_data\amo_data.db"
c = sqlite3.connect("file:" + p.replace("\\", "/") + "?mode=ro", uri=True)
rows = c.execute("SELECT id, superseded_by, content FROM memories WHERE status='merged' ORDER BY id DESC LIMIT 9").fetchall()
keeps = sorted({r[1] for r in rows})
out = {"merged": [], "keeps": {}}
for mid, keep, text in rows:
    out["merged"].append({"id": mid, "keep": keep, "content": text[:200]})
for keep in keeps:
    row = c.execute("SELECT content FROM memories WHERE id=?", (keep,)).fetchone()
    out["keeps"][keep] = (row[0][:400] if row else None)
c.close()
with open(r"D:/dafeiyu/astrbot/data/plugins/Astrbot_amoupup_data/work/semantic_sample.json", "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False, indent=1)
print("ok")
