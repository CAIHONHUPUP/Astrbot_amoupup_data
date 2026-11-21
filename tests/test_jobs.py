# -*- coding: utf-8 -*-
"""Phase 16：services/jobs.py 的 JobManager 契约（单飞 / 超时 / 隔离 / 退避 / stop）。

外加 MaintenanceEngine 的兼容接入：start() 仍首跑 60s（测试里改成 0.02s）、stop() 收尾。
"""
import asyncio
import time
import unittest

from services.jobs import JobManager


class JobManagerContractTests(unittest.TestCase):
    def test_register_is_idempotent(self):
        async def go():
            manager = JobManager(startup_delay=0)
            first = manager.register("a", lambda: None, 1)
            second = manager.register("a", lambda: None, 5)
            await manager.stop()
            return first is second, second.interval

        same, interval = asyncio.run(go())
        self.assertTrue(same)
        self.assertEqual(interval, 1.0)

    def test_report_has_contract_fields(self):
        async def go():
            manager = JobManager(startup_delay=0)
            manager.register("a", lambda: None, 2, timeout=1, max_backoff=8)
            rows = manager.report()
            await manager.stop()
            return rows

        rows = asyncio.run(go())
        self.assertEqual(len(rows), 1)
        expected = {
            "name", "interval", "running", "last_start", "last_end", "last_ok",
            "last_error", "consecutive_failures", "next_run",
        }
        self.assertTrue(expected.issubset(set(rows[0])), rows[0])
        self.assertFalse(rows[0]["running"])
        self.assertEqual(rows[0]["consecutive_failures"], 0)

    def test_next_run_immediate_and_interval(self):
        async def go():
            manager = JobManager(startup_delay=0)
            manager.register("now", lambda: None, 30, immediate=True)
            manager.register("later", lambda: None, 5)
            before = time.monotonic()
            manager.start()
            rows = {row["name"]: row for row in manager.report()}
            await manager.stop()
            return before, rows

        before, rows = asyncio.run(go())
        self.assertGreaterEqual(rows["now"]["next_run"], before)
        self.assertLess(rows["now"]["next_run"] - before, 0.2)
        self.assertGreaterEqual(rows["later"]["next_run"] - before, 4.8)
        self.assertLess(rows["later"]["next_run"] - before, 5.2)

    def test_run_now_single_flight_skips(self):
        async def go():
            gate = asyncio.Event()
            started = asyncio.Event()

            async def slow():
                started.set()
                await gate.wait()

            manager = JobManager(startup_delay=0)
            manager.register("slow", slow, 60)
            first_task = asyncio.create_task(manager.run_now("slow"))
            await asyncio.wait_for(started.wait(), 1)
            second = await manager.run_now("slow")
            gate.set()
            first = await first_task
            row = manager.report()[0]
            await manager.stop()
            return first, second, row

        first, second, row = asyncio.run(go())
        self.assertFalse(first["skipped"])
        self.assertTrue(first["ok"])
        self.assertTrue(second["skipped"])
        self.assertEqual(row["skipped"], 1)

    def test_timeout_cancels_and_records(self):
        async def go():
            async def slow():
                await asyncio.sleep(5)

            manager = JobManager(startup_delay=0)
            manager.register("slow", slow, 60, timeout=0.05)
            out = await manager.run_now("slow")
            row = manager.report()[0]
            await manager.stop()
            return out, row

        out, row = asyncio.run(go())
        self.assertFalse(out["ok"])
        self.assertIn("timeout", out["error"])
        self.assertEqual(row["timeouts"], 1)
        self.assertFalse(row["running"])
        self.assertIs(row["last_ok"], False)

    def test_exception_isolated(self):
        async def go():
            async def boom():
                raise ValueError("boom")

            manager = JobManager(startup_delay=0)
            manager.register("boom", boom, 60)
            out = await manager.run_now("boom")
            row = manager.report()[0]
            await manager.stop()
            return out, row

        out, row = asyncio.run(go())
        self.assertFalse(out["ok"])
        self.assertIn("ValueError: boom", out["error"])
        self.assertEqual(row["consecutive_failures"], 1)
        self.assertFalse(row["running"])

    def test_backoff_grows_after_failure(self):
        async def go():
            def fail():
                raise RuntimeError("x")

            manager = JobManager(startup_delay=0)
            manager.register("b", fail, 0.02, immediate=True, max_backoff=0.5)
            manager.start()
            deadline = time.time() + 1
            while manager.report()[0]["runs"] < 1 and time.time() < deadline:
                await asyncio.sleep(0.005)
            started = time.time()
            while manager.report()[0]["runs"] < 2 and time.time() < deadline:
                await asyncio.sleep(0.005)
            gap = time.time() - started
            row = manager.report()[0]
            await manager.stop()
            return gap, row

        gap, row = asyncio.run(go())
        self.assertGreaterEqual(gap, 0.03)
        self.assertLess(gap, 0.6)
        self.assertGreaterEqual(row["consecutive_failures"], 2)

    def test_stop_cancels_and_waits(self):
        async def go():
            started = asyncio.Event()

            async def forever():
                started.set()
                await asyncio.sleep(30)

            manager = JobManager(startup_delay=0)
            manager.register("f", forever, 60, immediate=True)
            manager.start()
            await asyncio.wait_for(started.wait(), 1)
            began = time.time()
            out = await manager.stop()
            took = time.time() - began
            return out, took, manager.report()[0], manager.task("f")

        out, took, row, task = asyncio.run(go())
        self.assertEqual(out["survivors"], 0)
        self.assertGreaterEqual(out["stopped"], 1)
        self.assertLess(took, 1.0)
        self.assertFalse(row["running"])
        self.assertTrue(task.done())

    def test_managed_spawn_returns_task(self):
        async def go():
            manager = JobManager(startup_delay=0)
            task = manager.spawn(asyncio.sleep(5), "sleep")
            out = await manager.stop()
            return task, out

        task, out = asyncio.run(go())
        self.assertIsNotNone(task)
        self.assertTrue(task.cancelled())
        self.assertEqual(out["survivors"], 0)


class _FakePlugin:
    db = object()
    settings = object()


class MaintenanceIntegrationTests(unittest.TestCase):
    def test_start_runs_once_then_stop(self):
        async def go():
            from services.maintenance_engine import MaintenanceEngine

            engine = MaintenanceEngine(_FakePlugin())
            calls = []

            async def fake_run_once():
                calls.append(1)
                return {"ok": True}

            engine.run_once = fake_run_once
            engine.jobs.startup_delay = 0.02
            engine.start()
            task = engine._task
            await asyncio.sleep(0.15)
            await engine.stop()
            return calls, task, engine._task

        calls, task, after = asyncio.run(go())
        self.assertGreaterEqual(len(calls), 1)
        self.assertIsNotNone(task)
        self.assertTrue(task.done())
        self.assertIsNone(after)

    def test_loop_compat_entry_waits_for_stop(self):
        async def go():
            from services.maintenance_engine import MaintenanceEngine

            engine = MaintenanceEngine(_FakePlugin())

            async def fake_run_once():
                return {}

            engine.run_once = fake_run_once
            engine.jobs.startup_delay = 60
            worker = asyncio.create_task(engine._loop())
            await asyncio.sleep(0.05)
            self.assertFalse(worker.done())
            await engine.stop()
            await asyncio.wait_for(worker, 1)
            return worker.done()

        self.assertTrue(asyncio.run(go()))


if __name__ == "__main__":
    unittest.main()
