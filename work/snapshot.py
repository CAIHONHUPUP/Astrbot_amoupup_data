# -*- coding: utf-8 -*-
"""真实库的一次性只读快照 + 备份。只写备份目录与临时目录，绝不写真实库。"""
import os, shutil, sqlite3, sys, time, json

SRC_DIR = r"D:\dafeiyu\astrbot\data\plugin_data\Astrbot_amoupup_data"
SRC = os.path.join(SRC_DIR, "amo_data.db")
STAMP = time.strftime("%Y%m%d-%H%M%S")
BACKUP = os.path.join(r"D:\dafeiyu\backup", "memory-recall-eval-" + STAMP)
os.makedirs(BACKUP, exist_ok=True)

# 1) 整目录 copy（含 wal/shm/backups），作为可回滚备份
dst_dir = os.path.join(BACKUP, "plugin_data", "Astrbot_amoupup_data")
shutil.copytree(SRC_DIR, dst_dir, dirs_exist_ok=True)

# 2) 一致性快照：只读连接 + sqlite backup API（WAL 下 copy 文件不保证一致）
snapshot = os.path.join(BACKUP, "amo_data.snapshot.db")
src = sqlite3.connect("file:" + SRC.replace("\\", "/") + "?mode=ro", uri=True, timeout=10)
dst = sqlite3.connect(snapshot)
src.backup(dst)
dst.close(); src.close()

chk = sqlite3.connect("file:" + snapshot.replace("\\", "/") + "?mode=ro", uri=True)
integrity = chk.execute("PRAGMA integrity_check").fetchone()[0]
counts = {
    "memories": chk.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
    "messages": chk.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
    "persons": chk.execute("SELECT COUNT(*) FROM persons").fetchone()[0],
}
chk.close()

# 3) 真实库指纹：证明没被改过（size + mtime + sha256）
import hashlib
h = hashlib.sha256()
with open(SRC, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
print(json.dumps({
    "stamp": STAMP, "backup": BACKUP, "snapshot": snapshot,
    "integrity": integrity, "counts": counts,
    "src_sha256": h.hexdigest(), "src_bytes": os.path.getsize(SRC),
    "src_mtime": os.path.getmtime(SRC),
}, ensure_ascii=False, indent=2))
