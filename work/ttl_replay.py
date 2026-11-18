# -*- coding: utf-8 -*-
"""TTL 回放：拿真实库只读快照，把「今天」往后拨，看生命周期会睡多少/过期多少。

用法：python work/ttl_replay.py --snapshot <snapshot.db> [--days 30,60,90,180,365]
不写真实库：先拷到临时目录，所有计算只在副本和内存里。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from models.memory_db import MemoryDB  # noqa: E402
from services import lifecycle  # noqa: E402
from services.memory_engine import MemoryEngine  # noqa: E402
from utils.config import PluginSettings  # noqa: E402

CONFIG = r"D:\dafeiyu\astrbot\data\config\Astrbot_amoupup_data_config.json"
REAL = r"D:\dafeiyu\astrbot\data\plugin_data\Astrbot_amoupup_data\amo_data.db"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--days", default="30,60,90,180,365")
    ap.add_argument("--bot-id", default="")
    args = ap.parse_args(argv)
    if os.path.abspath(args.snapshot).lower() == os.path.abspath(REAL).lower():
        raise SystemExit("拒绝在真实库上跑：先做只读快照")
    cfg = {}
    if os.path.exists(args.config):
        with open(args.config, encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
    settings = PluginSettings(cfg)
    tmp = tempfile.mkdtemp(prefix="ttl-replay-")
    try:
        copy = os.path.join(tmp, "snap.db")
        shutil.copy2(args.snapshot, copy)
        db = MemoryDB(copy, settings={"recall_fts_enabled": False})
        engine = MemoryEngine(db, settings)
        bot = args.bot_id or (db.bot_ids() or [""])[0]
        rows = [dict(r) for r in engine.dao.lifecycle_rows(bot, 5000)]
        overrides = {
            "noise_days": float(settings.get("lifecycle_noise_days", 30) or 30),
            "summary_days": float(settings.get("lifecycle_summary_days", 180) or 180),
            "expire_days": float(settings.get("lifecycle_expire_days", 365) or 365),
        }
        base_now = int(time.time())
        print("bot=%s active=%d overrides=%s" % (bot, len(rows), overrides))
        print("%6s %8s %8s  top kinds" % ("+days", "dormant", "expired"))
        for offset in [int(x) for x in args.days.split(",") if x.strip()]:
            now = base_now + offset * 86400
            dormant, expired = lifecycle.plan(rows, now, overrides)
            kinds = Counter(str(r.get("kind") or "") for r in rows
                            if int(r["id"]) in set(dormant))
            print("%6d %8d %8d  %s" % (offset, len(dormant), len(expired),
                                       ", ".join("%s:%d" % kv for kv in kinds.most_common(4))))
        db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
