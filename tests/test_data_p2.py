# -*- coding: utf-8 -*-
"""Phase 13（能记 P2）：api 响应封装 / engine 纯规则 / recall 四段与 A/B / fixtures 契约。

四组覆盖：
1. api：统一响应封装（ok/fail/normalize）、参数校验助手、路由仍是 34 条且 path 未变、
   所有 handler 都走响应助手（源码里不再出现裸 {"code": ...}）；
2. engine：compression_rules / fact_rules 的纯规则（不建库就能测）、向量队列的
   失败/去重/退避语义；
3. recall：四段（取候选/打分/多样性/兜底）各自单测 + 固定语料的 A/B 逐字节一致
   （tests/contracts/recall_golden.json 由改造前的代码生成）；
4. fixtures：固定时钟真的生效、临时库真的清理、桩设置真的可用。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import astrbot_stub  # noqa: E402

astrbot_stub.install()

from core import web_response as R  # noqa: E402
from core.web_handlers import WebHandlersMixin  # noqa: E402
from fixtures import FIXED_NOW, FakeSettings, TempStore, frozen_now, tp  # noqa: E402
from models.memory_db import MemoryDB  # noqa: E402
from services import compression_rules, fact_rules, recall_pipeline  # noqa: E402
from services.memory_engine import MemoryEngine  # noqa: E402
from services.retrieval_engine import RetrievalEngine  # noqa: E402
from support import recall_corpus  # noqa: E402
from utils.text_process import (  # noqa: E402
    DEFAULT_SETTINGS, _bm25_scores, _informative_terms, _query_chars, _recall_tokens,
)

GOLDEN = os.path.join(ROOT, "tests", "contracts", "recall_golden.json")
HANDLERS_DIR = os.path.join(ROOT, "core", "handlers")


def read(rel_path: str) -> str:
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def _row(rid, content, importance=0.5, created_days=0, hit_days=0, kind="fact"):
    return {
        "id": rid, "content": content, "kind": kind, "importance": importance,
        "created_at": FIXED_NOW - int(created_days * 86400),
        "last_hit_at": FIXED_NOW - int(hit_days * 86400),
        "hit_count": 0, "scope": "private", "group_id": "", "subject": "user",
    }


# ---------------------------------------------------------------------------
# 1) api 层
# ---------------------------------------------------------------------------

class ApiEnvelopeTests(unittest.TestCase):
    def test_success_shape(self):
        self.assertEqual(R.ok({"a": 1}), {"code": 0, "data": {"a": 1}})
        self.assertEqual(R.ok({"a": 1}, "已保存"),
                         {"code": 0, "data": {"a": 1}, "msg": "已保存"})
        self.assertEqual(R.ok({"a": 1}, applied=False),
                         {"code": 0, "data": {"a": 1}, "applied": False})

    def test_failure_shape(self):
        self.assertEqual(R.fail("缺少 uid"), {"code": 1, "msg": "缺少 uid"})
        self.assertEqual(R.forbidden("拒绝跨站请求"), {"code": 403, "msg": "拒绝跨站请求"})
        self.assertEqual(R.fail("保存失败", data={"ok": False}),
                         {"code": 1, "msg": "保存失败", "data": {"ok": False}})

    def test_result_helper(self):
        self.assertEqual(R.result(True, {"ok": True}, "已删除", "没有这条关系"),
                         {"code": 0, "data": {"ok": True}, "msg": "已删除"})
        self.assertEqual(R.result(False, {"ok": False}, "已删除", "没有这条关系"),
                         {"code": 1, "msg": "没有这条关系", "data": {"ok": False}})
        self.assertEqual(R.result(False, None, "已保存", "保存失败"),
                         {"code": 1, "msg": "保存失败"})

    def test_validation_helpers(self):
        payload = {"uid": " u1 ", "id": "12", "apply": "false", "confirm": "1"}
        self.assertEqual(R.text_field(payload, "missing", "uid"), "u1")
        self.assertEqual(R.text_field(payload, "missing"), "")
        self.assertEqual(R.int_field(payload, "id"), 12)
        self.assertEqual(R.int_field({"id": "abc"}, "id"), 0)
        self.assertEqual(R.int_field({"id": "abc"}, "id", 7), 7)
        self.assertFalse(R.flag_field(payload, "apply"))
        self.assertTrue(R.flag_field({}, "apply"))
        self.assertTrue(R.confirmed(payload))
        self.assertFalse(R.confirmed({"confirm": "no"}))
        self.assertEqual(R.missing("uid"), {"code": 1, "msg": "缺少 uid"})
        self.assertEqual(R.confirm_required("清空"),
                         {"code": 1, "msg": "清空需要确认：confirm=1"})

    def test_normalize_fills_missing_without_overwrite(self):
        self.assertEqual(R.normalize({"code": "0"}), {"code": 0, "data": None})
        self.assertEqual(R.normalize({"code": 0, "data": [1]}), {"code": 0, "data": [1]})
        self.assertEqual(R.normalize({"code": 1}), {"code": 1, "msg": "请求失败"})
        self.assertEqual(R.normalize({"code": "x"}), {"code": 1, "msg": "请求失败"})
        self.assertEqual(R.normalize("nope")["code"], 1)
        self.assertEqual(R.normalize({"code": 0, "data": None, "msg": "已保存"}),
                         {"code": 0, "data": None, "msg": "已保存"})


class RouteContractTests(unittest.TestCase):
    EXPECTED_PATHS = [
        "/status", "/profiles", "/profile", "/profile/save", "/profile/delete",
        "/persons", "/person", "/person/link", "/person/unlink", "/person/merge",
        "/memories", "/memory/save", "/memory/delete", "/memory/maintain",
        "/memory/backfill_platform", "/lifecycle", "/lifecycle/run",
        "/relations", "/relation/save", "/relation/delete",
        "/backup/now", "/backup/list", "/backup/export", "/backup/import", "/backup/restore",
        "/messages", "/groups", "/group/rename", "/message/delete", "/message/clear",
        "/logs", "/log/delete", "/log/clear", "/schedule", "/schedule/delete", "/maintenance",
    ]

    def test_handler_methods_still_34(self):
        names = sorted(name for name in dir(WebHandlersMixin) if name.startswith("api_"))
        self.assertEqual(len(names), 36)
        self.assertEqual(len(set(names)), 36)

    def test_route_table_paths_unchanged(self):
        text = read("core/web_api.py")
        block = text[text.index("routes = ["):]
        block = block[:block.index("\n        ]")]
        paths = re.findall(r'\("(/[^"]+)"', block)
        self.assertEqual(paths, self.EXPECTED_PATHS)
        self.assertEqual(len(paths), 36)

    def test_write_paths_are_subset_and_unchanged(self):
        text = read("core/web_api.py")
        block = text[text.index("WRITE_PATHS = frozenset({"):]
        block = block[:block.index("})")]
        writes = re.findall(r'"(/[^"]+)"', block)
        self.assertEqual(len(writes), 22, "写接口必须是 22 条（POST-only）")
        self.assertTrue(set(writes) <= set(self.EXPECTED_PATHS))

    def test_handlers_source_counted_as_34(self):
        total = 0
        for name in sorted(os.listdir(HANDLERS_DIR)):
            if not name.endswith(".py") or name == "__init__.py":
                continue
            body = read(os.path.join("core", "handlers", name))
            total += len(re.findall(r"^\s+async def api_[a-z_]+\(self\)", body, re.M))
        self.assertEqual(total, 36)

    def test_handlers_all_go_through_response_helpers(self):
        for name in sorted(os.listdir(HANDLERS_DIR)):
            if not name.endswith(".py") or name == "__init__.py":
                continue
            body = read(os.path.join("core", "handlers", name))
            self.assertNotIn('"code":', body, "%s 还有裸 code 字段，应该用 ok()/fail()" % name)
            self.assertNotIn('"msg":', body, "%s 还有裸 msg 字段，应该用 ok()/fail()" % name)

    def test_handler_files_stay_under_300_lines(self):
        for name in sorted(os.listdir(HANDLERS_DIR)):
            if not name.endswith(".py"):
                continue
            lines = read(os.path.join("core", "handlers", name)).count("\n") + 1
            self.assertLessEqual(lines, 300, "%s 超过 300 行（%d）" % (name, lines))


# ---------------------------------------------------------------------------
# 2) engine：纯规则 + 向量队列
# ---------------------------------------------------------------------------

class CompressionRuleTests(unittest.TestCase):
    def test_split_long_text_keeps_every_character(self):
        body = "第一句。第二句！第三句？" * 20
        pieces = compression_rules.split_long_text(body, 30)
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), body)
        self.assertTrue(all(len(piece) <= 30 for piece in pieces))
        self.assertEqual(compression_rules.split_long_text("", 10), [])
        self.assertEqual(compression_rules.split_long_text("短句。", 0), ["短句。"])

    def test_summary_group_key(self):
        self.assertEqual(
            compression_rules.summary_group_key("u", "group", "g1", "2026-11-16 聊天"),
            ("u", "2026-11-16", "group", "g1"),
        )
        self.assertIsNone(compression_rules.summary_group_key("u", "private", "", "没有日期"))

    def test_plan_summary_merge_keeps_longest_and_budget(self):
        rows = [
            {"id": 1, "content": "2026-11-16 " + "甲" * 20, "created_at": 10, "importance": 0.5},
            {"id": 2, "content": "2026-11-16 乙短", "created_at": 20, "importance": 0.6},
        ]
        keep, merged, absorbed = compression_rules.plan_summary_merge(rows, 4000)
        self.assertEqual(keep["id"], 1)
        self.assertIn("乙短", merged)
        self.assertEqual([row["id"] for row in absorbed], [2])

        tight = [
            {"id": 1, "content": "A" * 10, "created_at": 30, "importance": 0.5},
            {"id": 2, "content": "B" * 10, "created_at": 20, "importance": 0.5},
            {"id": 3, "content": "C" * 10, "created_at": 10, "importance": 0.5},
        ]
        keep2, _merged2, absorbed2 = compression_rules.plan_summary_merge(tight, 25)
        self.assertEqual(keep2["id"], 1)
        self.assertEqual([row["id"] for row in absorbed2], [2], "超预算必须停手，不能吞掉正文")

    def test_near_duplicate_pairs_respect_kind_and_scope(self):
        shared = ["词%d" % index for index in range(10)]
        tokens = [
            (1, "fact", "private", "", shared),
            (2, "fact", "private", "", shared + ["多一个"]),
            (3, "message", "private", "", shared),
            (4, "fact", "group", "g1", shared),
        ]
        self.assertEqual(compression_rules.near_duplicate_pairs(tokens, 0.7), [(1, 2)])


class FactRuleTests(unittest.TestCase):
    def _rows(self):
        return [
            {"id": 1, "uid": "u", "scope": "private", "group_id": "", "kind": "preference",
             "content": "我喜欢吃螺蛳粉", "importance": 0.4, "created_at": 10,
             "last_hit_at": 10, "hit_count": 0},
            {"id": 2, "uid": "u", "scope": "private", "group_id": "", "kind": "preference",
             "content": "我不喜欢吃螺蛳粉", "importance": 0.6, "created_at": 20,
             "last_hit_at": 20, "hit_count": 0},
            {"id": 3, "uid": "u", "scope": "private", "group_id": "", "kind": "expense",
             "content": "买了螺蛳粉 20 元", "importance": 0.9, "created_at": 30,
             "last_hit_at": 30, "hit_count": 0},
        ]

    def test_group_static_facts_skips_ledger(self):
        groups = fact_rules.group_static_facts(self._rows())
        self.assertEqual(len(groups), 1)
        items = list(groups.values())[0]
        self.assertEqual([row["id"] for row in items], [1, 2])

    def test_neg_pos_split_and_merge(self):
        items = list(fact_rules.group_static_facts(self._rows()).values())[0]
        neg, pos = fact_rules.split_neg_pos(items)
        self.assertEqual([row["id"] for row in neg], [2])
        self.assertEqual([row["id"] for row in pos], [1])
        self.assertEqual(fact_rules.pick_base(items)["id"], 2)
        self.assertEqual(fact_rules.bumped_importance(items), 0.65)
        self.assertIn("螺蛳粉", fact_rules.merged_text(items, fact_rules.pick_base(items)))

    def test_decay_targets_filters(self):
        rows = [
            {"kind": "fact", "last_hit_at": 10, "importance": 0.5, "id": 1},
            {"kind": "fact", "last_hit_at": 10, "importance": 0.1, "id": 2},
            {"kind": "fact", "last_hit_at": 90, "importance": 0.9, "id": 3},
            {"kind": "expense", "last_hit_at": 10, "importance": 0.9, "id": 4},
        ]
        targets = fact_rules.decay_targets(rows, cutoff=50)
        self.assertEqual([row["id"] for _imp, row in targets], [1])


class VectorQueueTests(unittest.TestCase):
    def _settings(self, **extra):
        settings = {
            "embedding_enabled": True, "embedding_backfill": False,
            "embedding_base": "", "embedding_model": "", "embedding_timeout": 0.1,
        }
        settings.update(extra)
        return settings

    def _open(self, prefix, **extra):
        tmp = tp(prefix)
        db = MemoryDB(os.path.join(tmp, "vec.db"))
        engine = MemoryEngine(db, self._settings(**extra))
        cleanups = [engine.close, db.close, lambda: shutil.rmtree(tmp, ignore_errors=True)]
        return engine, cleanups

    @staticmethod
    def _wait(condition, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return True
            time.sleep(0.01)
        return condition()

    def test_failure_is_counted_and_pending_cleared(self):
        engine, cleanups = self._open("vec-fail-")
        try:
            engine._enqueue_embedding(1, "文本")
            self.assertTrue(self._wait(lambda: engine._emb_failed >= 1))
            self.assertFalse(engine._emb_pending)
        finally:
            for fn in cleanups:
                fn()

    def test_store_failure_does_not_kill_worker(self):
        engine, cleanups = self._open("vec-store-")
        try:
            engine._embed = lambda text: [1.0, 0.0]
            engine._store_vec = mock.Mock(side_effect=RuntimeError("db busy"))
            engine._enqueue_embedding(2, "x")
            self.assertTrue(self._wait(lambda: engine._emb_failed >= 1))
            stored = []
            engine._store_vec = lambda memory_id, vec: stored.append(memory_id)
            engine._enqueue_embedding(3, "y")
            self.assertTrue(self._wait(lambda: 3 in stored), "向量 worker 不能被一次写库失败带死")
        finally:
            for fn in cleanups:
                fn()

    def test_down_backoff_blocks_immediate_retry(self):
        engine, cleanups = self._open("vec-down-",
                                      embedding_base="http://127.0.0.1:9",
                                      embedding_model="fake-model")
        try:
            with mock.patch("urllib.request.urlopen",
                            side_effect=OSError("connection refused")) as patched:
                self.assertEqual(engine._embed("hello"), [])
                self.assertGreater(engine._emb_down_until, time.time())
                self.assertTrue(engine._emb_warned)
                self.assertEqual(engine._embed("hello"), [])
                self.assertEqual(patched.call_count, 1, "60 秒内不该再打接口")
        finally:
            for fn in cleanups:
                fn()

    def test_enqueue_dedupes_and_drops_when_full(self):
        settings = self._settings(embedding_enabled=False)
        tmp = tp("vec-queue-")
        db = MemoryDB(os.path.join(tmp, "vec.db"))
        stop = threading.Event()
        dummy = threading.Thread(target=stop.wait, daemon=True)
        dummy.start()
        engine = MemoryEngine(db, settings)
        engine.settings["embedding_enabled"] = True
        engine._emb_thread = dummy  # 活着但不是 worker：队列入队可控
        try:
            engine._enqueue_embedding(42, "x")
            engine._enqueue_embedding(42, "x")
            self.assertEqual(engine._emb_queue.qsize(), 1)
            self.assertIn(42, engine._emb_pending)
            while not engine._emb_queue.empty():
                engine._emb_queue.get_nowait()
            for index in range(engine._emb_queue.maxsize):
                engine._emb_queue.put_nowait((10000 + index, "fill"))
            engine._enqueue_embedding(777, "y")
            self.assertNotIn(777, engine._emb_pending, "队列满时要回滚 pending")
        finally:
            stop.set()
            dummy.join(2)
            db.close()
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3) recall：四段 + A/B
# ---------------------------------------------------------------------------

def _features(query, rows, q_vec=None, coss=None, vec_pass=False):
    contents = [str(row["content"] or "") for row in rows]
    q_tokens = _recall_tokens(query)
    docs = [_recall_tokens(content) for content in contents]
    return {
        "contents": contents, "q_tokens": q_tokens, "docs": docs,
        "raw": _bm25_scores(q_tokens, docs), "q_vec": q_vec or [], "vec_map": {},
        "vec_weight": 0.45, "min_terms": 1,
        "infos": set(_informative_terms(query)), "qchars": set(_query_chars(query)),
        "coss": coss or {}, "best_cos": 0.0, "vec_pass": vec_pass,
        "vec_pass_ids": set(),
    }


class RecallStageTests(unittest.TestCase):
    def test_candidate_stage_helpers(self):
        self.assertEqual(recall_pipeline.clamp_pool(400, 0, 6), 400)
        self.assertEqual(recall_pipeline.clamp_pool(400, 10000, 6), 4000)
        self.assertEqual(recall_pipeline.clamp_pool(1, 0, 50), 50)
        self.assertEqual(recall_pipeline.clamp_recent_pool(None, 6), 600)
        self.assertEqual(recall_pipeline.clamp_recent_pool(0, 6), 600)
        self.assertEqual(recall_pipeline.clamp_recent_pool(99999, 6), 4000)
        rows = [{"id": 1}, {"id": 2}]
        extra = [{"id": 2}, {"id": 3}]
        self.assertEqual([row["id"] for row in recall_pipeline.merge_unique(rows, extra)],
                         [1, 2, 3])
        self.assertEqual(
            recall_pipeline.day_tag({"year": 2026, "month": 11, "day": 16}), "2026-11-16")
        self.assertTrue(recall_pipeline.is_date_hit(
            {"year": 2026, "month": 11, "day": 16}, "2026-11-16 的日总结"))
        self.assertFalse(recall_pipeline.is_date_hit(None, "2026-11-16"))

    def test_scoring_stage_pure(self):
        query = "螺蛳粉"
        rows = [
            _row(1, "主人喜欢吃螺蛳粉，尤其是加辣的", importance=0.8, created_days=60, hit_days=1),
            _row(2, "今天天气不错", importance=0.3, created_days=10, hit_days=1),
            _row(3, "螺蛳粉是柳州的小吃", importance=0.5, created_days=5, hit_days=2),
        ]
        features = _features(query, rows)
        ctx = {"text": query, "span": None, "now": FIXED_NOW,
               "half_life_days": 45.0, "importance_decay": 0.1}
        scored = recall_pipeline.score_rows(rows, features, ctx)
        # 2026-11-18：新鲜度改用 created_at（以前用 last_hit_at，被召回过的老记忆会显得很新），
        # 60 天前的高重要度记忆不再稳压 5 天前的同主题记忆，所以只断言集合与降序。
        self.assertEqual(sorted(item["id"] for item in scored), [1, 3], "没有关键词证据的行要被闸掉")
        self.assertGreaterEqual(scored[0]["score"], scored[1]["score"])
        self.assertGreater(scored[1]["score"], 0.6, "老记忆只是不再被 hit 刷新，不该掉到离谱")
        self.assertGreaterEqual(scored[0]["score"], scored[1]["score"])
        self.assertTrue(all(round(item["score"], 4) == item["score"] for item in scored))
        self.assertTrue(all(item["vec_score"] == 0.0 for item in scored))
        # 闸本身的语义：命中数够不够当证据
        _, _, strict_ok = recall_pipeline.keyword_strength({"a"}, {"a", "b", "c"}, {"a"}, 2)
        _, _, loose_ok = recall_pipeline.keyword_strength({"a"}, {"a", "b", "c"}, {"a"}, 0)
        self.assertFalse(strict_ok)
        self.assertTrue(loose_ok)

    def test_diversity_stage(self):
        items = [
            {"id": 1, "content": "我喜欢吃螺蛳粉"},
            {"id": 2, "content": "我不喜欢吃螺蛳粉"},
            {"id": 3, "content": "主人养了一只猫"},
        ]
        tokens = {1: {"螺蛳", "蛳粉"}, 2: {"螺蛳", "蛳粉"}, 3: {"猫"}}
        picked = recall_pipeline.pick_diverse(items, tokens, 5, 0.75)
        self.assertEqual([item["id"] for item in picked], [1, 3], "同一事实键只留最相关那条")
        plain = [{"id": 4, "content": "甲"}, {"id": 5, "content": "乙"}]
        plain_tokens = {4: {"a", "b"}, 5: {"a", "b"}}
        self.assertEqual(
            [item["id"] for item in recall_pipeline.pick_diverse(plain, plain_tokens, 5, 0.0)],
            [4, 5], "div_ratio=0 时关闭 jaccard 多样化")
        self.assertEqual(
            [item["id"] for item in recall_pipeline.pick_diverse(plain, plain_tokens, 5, 0.75)],
            [4], "jaccard 达到阈值只留一条")
        self.assertEqual([item["id"] for item in recall_pipeline.pick_diverse(items, tokens, 1, 0.75)],
                         [1], "limit 截断")

    def test_meta_fallback_stage(self):
        rows = [
            _row(1, "主人对芒果过敏", importance=0.9),
            _row(2, "主人喜欢喝冰美式", importance=0.2),
            _row(3, "主人养了一只猫", importance=0.5),
        ]
        picked = recall_pipeline.rank_meta_fallback(rows, FIXED_NOW, 45.0, 0.1, 2)
        self.assertEqual([item["id"] for item in picked], [1, 2])
        self.assertTrue(all(item["meta_fallback"] for item in picked))
        self.assertTrue(all(item["vec_score"] == 0.0 and item["matched_terms"] == 0
                            for item in picked))
        self.assertGreater(picked[0]["score"], picked[1]["score"])


class RecallGuardTests(unittest.TestCase):
    def _engine_with_spy(self, tmp):
        db = recall_corpus.build_db(tmp)
        calls = []

        class _Conn:
            def __init__(self, inner):
                self.inner = inner

            def execute(self, sql, params=None):
                calls.append((sql, tuple(params or ())))
                return self.inner.execute(sql, params or ())

            def executemany(self, sql, seq):
                calls.append((sql, "many"))
                return self.inner.executemany(sql, seq)

            def commit(self):
                return self.inner.commit()

        class _DB:
            def __init__(self, inner):
                self.inner = inner

            @property
            def conn(self):
                return _Conn(self.inner.conn)

            @property
            def lock(self):
                return self.inner.lock

            @property
            def fts_trigram(self):
                return False

            def dao_memory_count(self, *args):
                return 0

        engine = RetrievalEngine(_DB(db), dict(DEFAULT_SETTINGS), None, None)
        return db, engine, calls

    def test_sql_is_parameterized_and_capped(self):
        tmp = tp("recall-sql-")
        db = None
        try:
            db, engine, calls = self._engine_with_spy(tmp)
            with frozen_now():
                engine.recall("bot1", "u1", "螺蛳粉")
            sqls = [sql for sql, _params in calls]
            self.assertTrue(any("content LIKE ?" in sql for sql in sqls))
            self.assertTrue(any("LIMIT ?" in sql for sql in sqls))
            self.assertTrue(all("螺蛳粉" not in sql for sql in sqls),
                            "查询词只能走参数，不能拼进 SQL")
        finally:
            if db is not None:
                db.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_query_short_circuits(self):
        tmp = tp("recall-empty-")
        db = None
        try:
            db = recall_corpus.build_db(tmp)
            engine = recall_corpus.make_engine(db)
            engine.memory = mock.Mock()
            with frozen_now(), mock.patch(
                    "services.retrieval_engine._bm25_scores",
                    side_effect=AssertionError("空 query 不该跑 BM25")):
                items = engine.recall("bot1", "u1", "")
            self.assertTrue(items, "空 query 仍要按最近活跃兜底")
            self.assertTrue(all(item["vec_score"] == 0.0 for item in items))
            engine.memory._embed_cached.assert_not_called()
        finally:
            if db is not None:
                db.close()
            shutil.rmtree(tmp, ignore_errors=True)


class RecallAbTests(unittest.TestCase):
    def test_corpus_covers_required_scenarios(self):
        names = [scenario["name"] for scenario in recall_corpus.SCENARIOS]
        for required in ("ordinary", "meta_question", "group_isolation_group", "empty_db",
                         "no_result", "long_query", "tag_query", "time_window"):
            self.assertIn(required, names)
        self.assertGreaterEqual(len(names), 8)

    def test_golden_is_byte_identical(self):
        with open(GOLDEN, encoding="utf-8") as fh:
            expected = fh.read().rstrip("\n")
        tmp = tp("recall-ab-")
        try:
            data = recall_corpus.capture(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        current = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)
        self.assertEqual(current, expected,
                         "召回漂移：id 顺序 / 分数 / 条数必须与改造前逐字节一致")


# ---------------------------------------------------------------------------
# 4) fixtures 契约
# ---------------------------------------------------------------------------

class FixturesContractTests(unittest.TestCase):
    def test_tp_is_under_workspace_temp(self):
        path = tp("fx-")
        try:
            self.assertTrue(os.path.isdir(path))
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def test_frozen_now_patches_every_importer(self):
        import services.compression_engine as compression_mod
        import utils.text_process as text_mod
        with frozen_now() as clock:
            self.assertEqual(text_mod._now(), FIXED_NOW)
            self.assertEqual(clock.now(), FIXED_NOW)
            self.assertEqual(compression_mod._now(), FIXED_NOW)
        self.assertNotEqual(compression_mod._now(), FIXED_NOW, "退出后必须恢复真时钟")

    def test_frozen_now_changes_recall_scores(self):
        def scores(now_ts):
            tmp = tp("fx-recall-")
            db = None
            try:
                db = recall_corpus.build_db(tmp)
                engine = recall_corpus.make_engine(db)
                with frozen_now(now_ts):
                    return [(item["id"], item["score"])
                            for item in engine.recall("bot1", "u1", "螺蛳粉")]
            finally:
                if db is not None:
                    db.close()
                shutil.rmtree(tmp, ignore_errors=True)

        fresh = scores(FIXED_NOW)
        stale = scores(FIXED_NOW - 400 * 86400)
        self.assertEqual([item[0] for item in fresh], [item[0] for item in stale])
        self.assertNotEqual([item[1] for item in fresh], [item[1] for item in stale],
                            "时钟必须真的生效，否则 A/B 等于没测")

    def test_temp_store_cleans_up(self):
        holder = TempStore()
        with holder as store:
            self.assertTrue(os.path.isdir(holder.dir))
            store.remember("bot", "u", "主人喜欢螺蛳粉")
        self.assertFalse(os.path.exists(holder.dir), "临时库必须自动清理")

    def test_fake_settings_helpers(self):
        settings = FakeSettings({"a": "3", "b": "2.5", "c": "false"})
        self.assertEqual(settings._cfg_int("a"), 3)
        self.assertEqual(settings._cfg_float("b"), 2.5)
        self.assertFalse(settings._cfg_bool("c"))
        self.assertTrue(settings._cfg_bool("missing", True))
        self.assertEqual(settings.get("a"), "3")


if __name__ == "__main__":
    unittest.main()
