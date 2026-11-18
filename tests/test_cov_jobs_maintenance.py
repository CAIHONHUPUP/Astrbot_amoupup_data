# -*- coding: utf-8 -*-
"""覆盖率补齐：JobManager 边界分支、MaintenanceEngine 失败兜底、core/errors 翻译表。

这些用例只打「异常/空值/边界」分支：
* jobs.py：register 参数校验、cancel、stop 里的 cancel/wait 失败、survivors、
  spawn 建任务失败、_forget 取异常失败、is_closed 抛异常、jitter/drift 计算；
* maintenance_engine.py：每个可选步骤抛异常时都必须记进结果且不外溢，
  每日/每周/生命周期/语义巩固的「跳过」与「缓存失败」分支；
* core/errors.py：布尔/未知数字/空字符串的兜底文案。
"""
from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from services.jobs import JobManager
from services.maintenance_engine import MaintenanceEngine
from core import errors


class _FakeTask:
    """假 Task：用来构造 cancel / done 的异常与 survivors 分支。"""

    def __init__(self, done=False, cancel_raises=False):
        self._done = done
        self.cancel_raises = cancel_raises
        self.cancelled = 0

    def done(self):
        return self._done

    def cancel(self):
        self.cancelled += 1
        if self.cancel_raises:
            raise RuntimeError("cancel boom")


class _Coro:
    def __init__(self, close_raises=False):
        self.closed = 0
        self.close_raises = close_raises

    def close(self):
        self.closed += 1
        if self.close_raises:
            raise RuntimeError("close boom")


class JobsEdgeTests(unittest.TestCase):
    def test_register_rejects_bad_arguments(self) -> None:
        manager = JobManager(default_timeout=7)
        with self.assertRaises(ValueError):
            manager.register("zero", lambda: None, 0)
        with self.assertRaises(TypeError):
            manager.register("badfn", None, 5)
        # max_backoff 比 interval 还小 -> 抬到 interval；不传 -> interval * 8
        clamped = manager.register("clamped", lambda: None, 2, max_backoff=1)
        self.assertEqual(clamped.max_backoff, 2.0)
        default_backoff = manager.register("dflt", lambda: None, 3)
        self.assertEqual(default_backoff.max_backoff, 24.0)
        # timeout 没给时用 manager 的 default_timeout
        self.assertEqual(default_backoff.timeout, 7.0)

    def test_register_after_start_creates_task(self) -> None:
        async def go():
            manager = JobManager(startup_delay=0)
            manager.register("first", lambda: None, 30)
            manager.start()
            late = manager.register("late", lambda: None, 30)
            task = manager.task("late")
            await manager.stop()
            return late.name, task

        name, task = asyncio.run(go())
        self.assertEqual(name, "late")
        self.assertIsNotNone(task)

    def test_start_resets_closed_event_and_wait_releases(self) -> None:
        async def go():
            manager = JobManager()
            await manager.stop()
            closed_before = manager._closed.is_set()
            manager.start()
            closed_after = manager._closed.is_set()
            waiter = asyncio.create_task(manager.wait())
            await asyncio.sleep(0)
            await manager.stop()
            await asyncio.wait_for(waiter, 1)
            return closed_before, closed_after, waiter.done()

        before, after, done = asyncio.run(go())
        self.assertTrue(before)
        self.assertFalse(after)
        self.assertTrue(done)

    def test_stop_survives_cancel_and_wait_errors(self) -> None:
        async def go():
            manager = JobManager(tag="cov")
            manager.register("a", lambda: None, 30)
            fake = _FakeTask(done=False, cancel_raises=True)
            manager._jobs["a"].task = fake
            with mock.patch("services.jobs.asyncio.wait",
                            side_effect=RuntimeError("wait boom")):
                out = await manager.stop()
            return out, fake.cancelled

        out, cancelled = asyncio.run(go())
        self.assertEqual(cancelled, 1)
        self.assertEqual(out["stopped"], 1)
        self.assertEqual(out["survivors"], 1)

    def test_cancel_variants(self) -> None:
        async def go():
            manager = JobManager()
            unknown = manager.cancel("nope")
            job = manager.register("a", lambda: None, 30)
            no_task = manager.cancel("a")
            task = asyncio.create_task(asyncio.sleep(30))
            job.task = task
            cancelled = manager.cancel("a")
            after_task = job.task
            after_next = job.next_run
            await asyncio.gather(task, return_exceptions=True)
            return unknown, no_task, cancelled, after_task, after_next

        unknown, no_task, cancelled, after_task, after_next = asyncio.run(go())
        self.assertFalse(unknown)
        self.assertFalse(no_task)
        self.assertTrue(cancelled)
        self.assertIsNone(after_task)
        self.assertIsNone(after_next)

    def test_run_now_unknown_and_summary_and_spawned(self) -> None:
        async def go():
            manager = JobManager()
            out = await manager.run_now("nope")
            manager.register("a", lambda: None, 30)
            summary = manager.summary()
            return out, summary, manager.spawned

        out, summary, spawned = asyncio.run(go())
        self.assertIsNone(out)
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["running"], 0)
        self.assertEqual(summary["failing"], 0)
        self.assertEqual(len(summary["jobs"]), 1)
        self.assertEqual(spawned, set())

    def test_spawn_returns_none_when_task_creation_fails(self) -> None:
        async def go():
            def factory(name, coro):
                raise RuntimeError("no running loop")

            first = _Coro()
            manager = JobManager(task_factory=factory)
            out = manager.spawn(first, "x")
            second = _Coro(close_raises=True)
            manager2 = JobManager(task_factory=factory)
            out2 = manager2.spawn(second, "y")
            return out, first.closed, out2, second.closed

        out, first_closed, out2, second_closed = asyncio.run(go())
        self.assertIsNone(out)
        self.assertEqual(first_closed, 1)
        self.assertIsNone(out2)
        self.assertEqual(second_closed, 1)

    def test_forget_ignores_cancelled_and_logs_exception(self) -> None:
        manager = JobManager(tag="cov")

        class _Cancelled:
            def cancelled(self):
                return True

        class _NoResult:
            def cancelled(self):
                return False

            def exception(self):
                raise RuntimeError("no result")

        class _Boom:
            def cancelled(self):
                return False

            def exception(self):
                return ValueError("boom")

        manager._forget(_Cancelled())
        manager._forget(_NoResult())
        manager._forget(_Boom())
        self.assertEqual(manager.spawned, set())

    def test_stopping_checks_closed_and_checker(self) -> None:
        closed = JobManager()
        closed._closed.set()
        self.assertTrue(closed._stopping())

        def boom():
            raise RuntimeError("is_closed boom")

        self.assertFalse(JobManager(is_closed=boom)._stopping())
        self.assertTrue(JobManager(is_closed=lambda: True)._stopping())
        self.assertFalse(JobManager(is_closed=lambda: False)._stopping())

    def test_ensure_task_returns_existing_and_jitter(self) -> None:
        async def go():
            manager = JobManager(startup_delay=1.0, jitter=0.5)
            job = manager.register("a", lambda: None, 30)
            first = manager._ensure_task(job)
            second = manager._ensure_task(job)
            await manager.stop()
            return first, second

        first, second = asyncio.run(go())
        self.assertIs(first, second)

        manager = JobManager(jitter=0.5)
        wait = manager._jittered(10)
        self.assertGreaterEqual(wait, 5.0)
        self.assertLessEqual(wait, 15.0)
        job = manager.register("j", lambda: None, 10)
        job.jitter = 0.2
        wait_job = manager._jittered(10, job)
        self.assertGreaterEqual(wait_job, 8.0)
        self.assertLessEqual(wait_job, 12.0)

    def test_next_delay_backoff_floor_and_drift(self) -> None:
        manager = JobManager()
        job = manager.register("j", lambda: None, 10, max_backoff=50)
        job.consecutive_failures = 3
        self.assertEqual(manager._next_delay(job, 0), 50.0)

        drifting = JobManager(drift_correct=True)
        drift_job = drifting.register("d", lambda: None, 10)
        self.assertEqual(drifting._next_delay(drift_job, 3.5), 6.5)

        tiny = drifting.register("t", lambda: None, 0.5)
        self.assertEqual(drifting._next_delay(tiny, 10), 0.5)

    def test_loop_exits_after_stop_flag_during_sleep(self) -> None:
        async def go():
            manager = JobManager(startup_delay=0)
            manager.register("a", lambda: None, 0.01, immediate=True)
            original_sleep = asyncio.sleep

            async def fast_sleep(delay):
                manager._closed.set()
                await original_sleep(0)

            with mock.patch("services.jobs.asyncio.sleep", side_effect=fast_sleep):
                manager.start()
                task = manager.task("a")
                await asyncio.wait_for(task, 1)
            await manager.stop()
            return task.done()

        self.assertTrue(asyncio.run(go()))


class _Settings(dict):
    chat_days = 14
    compress_after = 3
    log_days = 7

    def __init__(self, **values):
        super().__init__({
            "embedding_enabled": False,
            "lifecycle_enabled": False,
            "semantic_consolidation_enabled": False,
            "backup_daily": True,
            "backup_keep": 7,
            "recall_entity_graph": True,
        })
        self.update(values)


class _Cache:
    def __init__(self, get_result=None, get_raises=False, set_raises=False):
        self.get_result = get_result
        self.get_raises = get_raises
        self.set_raises = set_raises
        self.sets = []

    def cache_get(self, bot_id, key):
        if self.get_raises:
            raise RuntimeError("cache get boom")
        return self.get_result

    def cache_set(self, bot_id, key, value, ttl=None):
        if self.set_raises:
            raise RuntimeError("cache set boom")
        self.sets.append((key, value, ttl))


def _raise(*args, **kwargs):
    raise RuntimeError("component boom")


class _Cursor:
    def fetchone(self):
        return {"c": 2}


class _Conn:
    def __init__(self, fail_status=False):
        self.fail_status = fail_status
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.fail_status and "COALESCE(status" in sql:
            raise RuntimeError("status boom")
        return _Cursor()


def make_plugin(cache=None, conn=None, settings=None):
    plugin = SimpleNamespace(
        db=SimpleNamespace(lock=threading.Lock(), conn=conn or _Conn()),
        settings=settings or _Settings(),
        cache=cache or _Cache(),
        messages=SimpleNamespace(compress_messages=lambda *a, **k: {"ok": True}),
        logs=SimpleNamespace(purge_logs=lambda *a, **k: 1),
        compression=SimpleNamespace(
            compress_memories=lambda *a, **k: {"applied": True},
            archive_stale=lambda *a, **k: {"archived": 0},
            consolidate_noise=lambda *a, **k: {"merged": 0},
        ),
        backup=SimpleNamespace(backup_now=lambda keep: "/tmp/x.db"),
        memory=SimpleNamespace(
            dao=object(),
            backfill_embeddings=lambda limit: 0,
            run_lifecycle=lambda bot_id, apply=True: {},
        ),
    )
    plugin._bot_id = lambda payload, write=False: "b"
    plugin.mem = SimpleNamespace(consolidate_semantic=_async_ok)
    return plugin


async def _async_ok(bot_id):
    return {"ok": True}


class MaintenanceFailureTests(unittest.TestCase):
    def test_start_without_event_loop_returns(self) -> None:
        engine = MaintenanceEngine(make_plugin())
        engine.start()
        self.assertIsNone(engine._task)

    def test_start_short_circuits_when_task_running(self) -> None:
        async def go():
            engine = MaintenanceEngine(make_plugin())
            task = asyncio.create_task(asyncio.sleep(30))
            engine._task = task
            engine.start()
            same = engine._task is task
            await engine.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return same

        self.assertTrue(asyncio.run(go()))

    def test_stop_swallows_job_manager_error(self) -> None:
        async def go():
            engine = MaintenanceEngine(make_plugin())

            async def boom():
                raise RuntimeError("stop boom")

            engine.jobs.stop = boom
            await engine.stop()
            return engine._task

        self.assertIsNone(asyncio.run(go()))

    def test_run_once_records_every_component_error(self) -> None:
        async def go():
            settings = _Settings(embedding_enabled=True, lifecycle_enabled=True,
                                 semantic_consolidation_enabled=True)
            plugin = make_plugin(settings=settings)
            plugin.memory.backfill_embeddings = _raise
            engine = MaintenanceEngine(plugin)
            engine.retention = _raise

            async def boom(*args, **kwargs):
                raise RuntimeError("async boom")

            engine._maybe_weekly_cleanup = boom
            engine._maybe_daily_backup = boom
            engine._maybe_lifecycle = _raise
            engine._maybe_semantic = boom
            return await engine.run_once()

        result = asyncio.run(go())
        self.assertIn("component boom", result["chat_error"])
        self.assertIn("component boom", result["lifecycle_error"])
        self.assertIn("async boom", result["semantic_error"])

    def test_semantic_skip_and_cache_set_failure(self) -> None:
        async def go():
            engine = MaintenanceEngine(make_plugin(cache=_Cache(get_result=0)))
            with mock.patch("services.maintenance_engine.lifecycle.should_run",
                            return_value=False) as should_run:
                skipped = await engine._maybe_semantic("b")
            calls = should_run.call_count

            cache2 = _Cache(get_result=0, set_raises=True)
            engine2 = MaintenanceEngine(make_plugin(cache=cache2))
            with mock.patch("services.maintenance_engine.lifecycle.should_run",
                            return_value=True):
                ran = await engine2._maybe_semantic("b")
            return skipped, calls, ran

        skipped, calls, ran = asyncio.run(go())
        self.assertTrue(skipped["skipped"])
        self.assertEqual(calls, 1)
        self.assertEqual(ran, {"ok": True})

    def test_semantic_survives_cache_read_failure(self) -> None:
        """修复回归：cache_get 抛异常时 last 必须按 0 处理，不能 UnboundLocalError。"""
        async def go():
            engine = MaintenanceEngine(make_plugin(cache=_Cache(get_raises=True)))
            with mock.patch("services.maintenance_engine.lifecycle.should_run",
                            return_value=False) as should_run:
                out = await engine._maybe_semantic("b")
            return out, should_run.call_args

        out, call = asyncio.run(go())
        self.assertTrue(out["skipped"])
        self.assertEqual(out["last"], 0)
        self.assertEqual(call.args[0], 0)

    def test_lifecycle_skips_and_records_graph_error(self) -> None:
        engine = MaintenanceEngine(make_plugin(cache=_Cache(get_raises=True)))
        with mock.patch("services.maintenance_engine.lifecycle.should_run",
                        return_value=False) as should_run:
            skipped = engine._maybe_lifecycle("b")
        calls = should_run.call_count

        cache2 = _Cache(get_result=0, set_raises=True)
        engine2 = MaintenanceEngine(make_plugin(cache=cache2))
        with mock.patch("services.maintenance_engine.lifecycle.should_run",
                        return_value=True), \
             mock.patch("services.maintenance_engine.entity_graph.backfill",
                        side_effect=RuntimeError("graph boom")):
            out = engine2._maybe_lifecycle("b")

        self.assertTrue(skipped["skipped"])
        self.assertEqual(calls, 1)
        self.assertIn("graph boom", out["graph_error"])

    def test_retention_records_component_errors(self) -> None:
        plugin = make_plugin()
        plugin.messages.compress_messages = _raise
        plugin.logs.purge_logs = _raise
        engine = MaintenanceEngine(plugin)
        result = engine.retention("b", 14, 3, 7)
        self.assertIn("component boom", result["chat_error"])
        self.assertIn("component boom", result["log_error"])

    def test_weekly_cleanup_skips_and_survives_errors(self) -> None:
        async def go():
            skipped_engine = MaintenanceEngine(make_plugin(cache=_Cache(get_result=True)))
            skipped = await skipped_engine._maybe_weekly_cleanup("b")

            plugin = make_plugin(cache=_Cache(get_raises=True, set_raises=True))
            plugin.compression.compress_memories = _raise
            plugin.compression.archive_stale = _raise
            plugin.compression.consolidate_noise = _raise
            engine = MaintenanceEngine(plugin)
            out = await engine._maybe_weekly_cleanup("b")
            return skipped, out

        skipped, out = asyncio.run(go())
        self.assertTrue(skipped["skipped"])
        self.assertIn("component boom", out["compress"]["error"])
        self.assertIn("component boom", out["archive"]["error"])
        self.assertIn("component boom", out["consolidate"]["error"])

    def test_daily_backup_survives_cache_errors(self) -> None:
        async def go():
            cache = _Cache(get_raises=True, set_raises=True)
            engine = MaintenanceEngine(make_plugin(cache=cache))
            return await engine._maybe_daily_backup("b")

        self.assertEqual(asyncio.run(go()), "/tmp/x.db")

    def test_stats_with_filter_and_status_query_error(self) -> None:
        conn = _Conn()
        engine = MaintenanceEngine(make_plugin(conn=conn))
        filtered = engine.stats("b")
        assert any("WHERE 1=1" in sql for sql, _ in conn.sql)
        self.assertEqual(filtered["memories"], 2)
        self.assertEqual(filtered["memories_active"], 2)

        conn2 = _Conn(fail_status=True)
        engine2 = MaintenanceEngine(make_plugin(conn=conn2))
        safe = engine2.stats("b")
        self.assertEqual(safe["profiles"], 2)
        self.assertNotIn("memories_active", safe)

    def test_loop_returns_when_task_missing(self) -> None:
        async def go():
            engine = MaintenanceEngine(make_plugin())
            engine.jobs.task = lambda name: None
            await engine._loop()
            await engine.jobs.stop()
            return engine._task

        self.assertIsNone(asyncio.run(go()))

    def test_loop_handles_cancellation(self) -> None:
        async def go():
            engine = MaintenanceEngine(make_plugin())
            engine.jobs.startup_delay = 60
            worker = asyncio.create_task(engine._loop())
            await asyncio.sleep(0.05)
            await engine.stop()
            await asyncio.wait_for(worker, 1)
            return worker.done()

        self.assertTrue(asyncio.run(go()))


class ErrorCodeFallbackTests(unittest.TestCase):
    def test_code_name_handles_non_numeric(self) -> None:
        self.assertEqual(errors.code_name(object(), "dflt"), "dflt")
        self.assertEqual(errors.code_name(0), errors.CODE_OK)

    def test_code_number_handles_bool_and_unknown(self) -> None:
        self.assertIsNone(errors.code_number(True))
        self.assertEqual(errors.code_number(True, 9), 9)
        self.assertIsNone(errors.code_number("not-registered"))
        self.assertEqual(errors.code_number(1), 1)

    def test_is_known_checks_registry(self) -> None:
        self.assertTrue(errors.is_known(errors.CODE_OK))
        self.assertFalse(errors.is_known("nope"))
        self.assertFalse(errors.is_known(None))

    def test_describe_code_covers_unknown_shapes(self) -> None:
        self.assertEqual(errors.describe_code(True), "未知错误码：True")
        with mock.patch.dict(errors.DESCRIPTIONS, {}, clear=True):
            self.assertEqual(errors.describe_code(9999), "未登记的数字码：9999")
        self.assertEqual(errors.describe_code(""), "未知错误码：空")
        self.assertEqual(errors.describe_code("nope"), "未知错误码：nope")
        self.assertEqual(errors.describe_code(0), "成功（code=0）")


if __name__ == "__main__":
    unittest.main()
