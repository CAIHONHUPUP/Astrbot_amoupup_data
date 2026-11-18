# -*- coding: utf-8 -*-
"""召回 A/B 固定语料（Phase 13 P2）。

同一批 fixture 数据 + 同一批 query，改造前后 recall 的 id 顺序 / 分数 / 条数必须逐字节一致。
语料全部显式指定 id 与时间戳，并把「现在」冻结在 FIXED_NOW，任何跑动都得到同一份输出；
非空库每个场景都新建一份库（recall 会写 hit_count/last_hit_at），所以场景之间互不影响。
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

from models.memory_db import MemoryDB
from services.retrieval_engine import RetrievalEngine
from utils.text_process import DEFAULT_SETTINGS

_CN = timezone(timedelta(hours=8))
#: 冻结的「现在」：2026-11-16 21:30 +08:00（周一）
FIXED_NOW = int(datetime(2026, 11, 16, 21, 30, tzinfo=_CN).timestamp())
FIXED_DT = datetime(2026, 11, 16, 21, 30)
BOT = "bot1"
USER = "u1"
OTHER = "u2"

_DAY = 86400
_LONG_BODY = "我们聊了很多关于工作和生活的事情，" * 40


def _row(rid, content, *, uid=USER, kind="fact", subject="user", scope="private",
         group_id="", importance=0.5, hit_count=0, age_days=0.0, last_hit_age=None,
         tags="", status="active"):
    created = FIXED_NOW - int(age_days * _DAY)
    hit_age = age_days if last_hit_age is None else last_hit_age
    return {
        "id": rid, "bot_id": BOT, "uid": uid, "scope": scope, "group_id": group_id,
        "kind": kind, "subject": subject, "content": content,
        "importance": importance, "hit_count": hit_count,
        "created_at": created, "last_hit_at": FIXED_NOW - int(hit_age * _DAY),
        "status": status, "tags": tags,
    }


#: 15 条固定记忆：普通事实 / 近重复偏好 / 群聊 / 日总结 / 摘要 / 标签 / 长文 / 他人
ROWS = [
    _row(1, "主人喜欢吃螺蛳粉，尤其是加辣的", importance=0.8, hit_count=3, age_days=30, last_hit_age=1),
    _row(2, "螺蛳粉真好吃，昨天又去吃了", importance=0.5, hit_count=1, age_days=20, last_hit_age=3),
    _row(3, "主人养了一只叫团子的橘猫", importance=0.6, hit_count=2, age_days=60, last_hit_age=5),
    _row(4, "群里约好周末一起爬山，山脚下集合", scope="group", group_id="g1", importance=0.5,
         age_days=4, last_hit_age=2),
    _row(5, "群里聊到加班到很晚，回家路上还下着雨", scope="group", group_id="g1",
         importance=0.4, age_days=8, last_hit_age=6),
    _row(6, "2026-11-16 昨天下午我们聊了搬家的事，还说要请货车", kind="daily_digest",
         importance=0.7, hit_count=1, age_days=1, last_hit_age=1),
    _row(7, "2026-11-15 前天晚上你熬夜写代码到三点", kind="daily_digest", importance=0.55,
         age_days=2, last_hit_age=2),
    _row(8, "主人对芒果过敏，不能吃", importance=0.9, hit_count=4, age_days=90, last_hit_age=12),
    _row(9, "#夜宵 主人喜欢凌晨点烤串", importance=0.4, age_days=15, last_hit_age=7, tags="#夜宵"),
    _row(10, "主人最近在学吉他，练了两个月还是弹不好", importance=0.5, age_days=25, last_hit_age=9),
    _row(11, "别人说的秘密：量子力学作业没写完", uid=OTHER, importance=0.5, age_days=6,
         last_hit_age=4),
    _row(12, "2026-11-16 晚上我们聊了很多，从工作聊到未来", kind="conversation_summary",
         importance=0.6, age_days=1, last_hit_age=1),
    _row(13, _LONG_BODY, kind="conversation_summary", importance=0.45, age_days=12,
         last_hit_age=10),
    _row(14, "我喜欢喝冰美式，不加糖", kind="preference", importance=0.55, age_days=40,
         last_hit_age=11),
    _row(15, "螺蛳粉里的酸笋是灵魂，闻着臭吃着香", importance=0.65, hit_count=1, age_days=10,
         last_hit_age=2),
]

_LONG_QUERY = ("我们聊了很多关于工作和生活的事情，昨天下午还说起搬家，"
               "顺便问一句螺蛳粉到底好不好吃，" * 4)

#: 8 组场景（覆盖普通/元问题/群聊隔离/空库/无结果/长 query/标签/时间窗 + 两条边界）
SCENARIOS = [
    {"name": "ordinary", "query": "螺蛳粉", "limit": 6},
    {"name": "ordinary_limit3", "query": "螺蛳粉", "limit": 3},
    {"name": "meta_question", "query": "你还记得我吗", "limit": 6},
    {"name": "group_isolation_group", "query": "爬山", "scope": "group", "group_id": "g1", "limit": 6},
    {"name": "group_isolation_private", "query": "爬山", "scope": "private", "limit": 6},
    {"name": "empty_db", "query": "记得什么", "limit": 6, "empty": True},
    {"name": "no_result", "query": "量子力学作业", "limit": 6},
    {"name": "long_query", "query": _LONG_QUERY, "limit": 6},
    {"name": "tag_query", "query": "#夜宵", "limit": 6},
    {"name": "time_window", "query": "昨天我们干了什么", "limit": 6},
    {"name": "empty_query", "query": "", "limit": 6},
]

_COLUMNS = ("id", "bot_id", "uid", "scope", "group_id", "kind", "subject", "content",
            "importance", "hit_count", "created_at", "last_hit_at", "status", "tags")


def build_db(directory, rows=None):
    """在 directory 里建一份固定语料库（rows=None 用 ROWS，[] 表示空库）。"""
    os.makedirs(directory, exist_ok=True)
    db = MemoryDB(os.path.join(directory, "recall.db"), settings=dict(DEFAULT_SETTINGS))
    data = ROWS if rows is None else list(rows)
    if data:
        marks = ",".join("?" for _ in _COLUMNS)
        sql = "INSERT INTO memories (%s) VALUES (%s)" % (", ".join(_COLUMNS), marks)
        db.conn.executemany(sql, [tuple(row[col] for col in _COLUMNS) for row in data])
        db.conn.commit()
    return db


@contextlib.contextmanager
def frozen_now(now_ts=FIXED_NOW, now_dt=FIXED_DT):
    """把「现在」钉死：retrieval_engine / text_process 的 _now 与 time_query 用的 clock.now。"""
    import services.retrieval_engine as retrieval_mod
    import utils.clock as clock_mod
    import utils.text_process as text_mod

    saved = (retrieval_mod._now, text_mod._now, clock_mod.now)
    try:
        retrieval_mod._now = lambda: int(now_ts)
        text_mod._now = lambda: int(now_ts)
        clock_mod.now = lambda explicit="": now_dt
        yield
    finally:
        retrieval_mod._now, text_mod._now, clock_mod.now = saved


def make_engine(db, settings=None):
    base = dict(DEFAULT_SETTINGS)
    if settings:
        base.update(settings)
    return RetrievalEngine(db, base, persons=None, memory=None)


def run_scenario(scenario, tmpdir):
    """跑一个场景，返回可逐字节比较的归一化结果。"""
    tmp = tempfile.mkdtemp(prefix="recall-", dir=tmpdir)
    db = None
    try:
        db = build_db(tmp, [] if scenario.get("empty") else None)
        engine = make_engine(db)
        kwargs = {"limit": int(scenario.get("limit", 6))}
        for key in ("scope", "group_id"):
            if scenario.get(key):
                kwargs[key] = scenario[key]
        with frozen_now():
            items = engine.recall(BOT, USER, scenario.get("query", ""), **kwargs)
        return [
            {
                "id": int(item["id"]),
                "score": float(item["score"]),
                "matched_terms": int(item.get("matched_terms") or 0),
                "vec_score": float(item.get("vec_score") or 0.0),
                "content": str(item.get("content") or ""),
            }
            for item in items
        ]
    finally:
        if db is not None:
            db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def capture(tmpdir=None):
    """跑全部场景，返回 {scenario_name: [items...]}（字典顺序同 SCENARIOS）。"""
    base = tmpdir or tempfile.mkdtemp(prefix="recall-capture-")
    owned = tmpdir is None
    try:
        return {item["name"]: run_scenario(item, base) for item in SCENARIOS}
    finally:
        if owned:
            shutil.rmtree(base, ignore_errors=True)


def capture_text():
    """稳定序列化：A/B 比较的是这串字节。"""
    return json.dumps(capture(), ensure_ascii=False, indent=1, sort_keys=True)
