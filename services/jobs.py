# -*- coding: utf-8 -*-
"""统一后台任务管理器（Phase 16 JobManager 契约，能记版）。

三个插件各有一份实现，API 与语义保持一致：

    register(name, fn, interval, *, immediate=False, timeout=None, max_backoff=None)
    start()
    async stop(timeout=3.0)
    async run_now(name)
    report() -> list[dict]

语义（三边同一份）：
- 单飞：同一 job 上一轮还没结束就跳过本轮，累计 skipped，不并发；
- 超时：单次运行超过 timeout 用 asyncio.wait_for 取消并记 timeout；
- 隔离：job 抛出的异常只记 last_error / consecutive_failures，绝不外溢；
- 退避：连续失败按 interval * 2**failures 指数退避，封顶 max_backoff（默认 8 倍间隔）；
- stop：取消全部 job 与受管 spawn，等待收尾（默认 3 秒），返回 stopped / survivors。

各插件的调度底座允许不同（本文件是陪伴用的 asyncio 版）：
- 记忆：manager 自建任务，首跑延迟 60 秒，之后 3600s，run_once 内部仍用 to_thread；
- 陪伴：manager 自建任务，首跑延迟 4 秒，之后 cfg loop_interval_seconds（默认 30s）；
- 守护：沿用 core/runtime.spawn 的受管任务 + 固定频率纠偏 + 5% 抖动，见 core/scheduler.py。
差别只在“下一次什么时候跑”，单飞/超时/退避/观测的语义三边完全一致。
"""
from __future__ import annotations

import asyncio
import inspect
import random
import time

try:  # 包内运行时
    from ..utils.text_process import logger as _logger
except ImportError:  # 顶层导入（测试/脚本）
    from utils.text_process import logger as _logger


class Job:
    """单个定时任务的运行状态；只由 JobManager 读写。"""

    def __init__(self, name, fn, interval, *, immediate, timeout, max_backoff):
        self.name = name
        self.fn = fn
        self.interval = float(interval)
        self.immediate = bool(immediate)
        self.timeout = None if timeout is None else float(timeout)
        self.max_backoff = float(max_backoff)
        self.jitter = None
        self.task = None
        self.running = False
        self.runs = 0
        self.skipped = 0
        self.timeouts = 0
        self.last_start = None
        self.last_end = None
        self.last_ok = None
        self.last_error = ""
        self.consecutive_failures = 0
        self.next_run = None

    def snapshot(self):
        return {
            "name": self.name,
            "interval": self.interval,
            "running": bool(self.running),
            "last_start": self.last_start,
            "last_end": self.last_end,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "consecutive_failures": int(self.consecutive_failures),
            "next_run": self.next_run,
            "runs": int(self.runs),
            "skipped": int(self.skipped),
            "timeouts": int(self.timeouts),
        }


class JobManager:
    """统一后台任务管理器；契约见模块 docstring。"""

    def __init__(self, *, logger=None, tag="amo", startup_delay=0.0,
                 jitter=0.0, drift_correct=False, is_closed=None,
                 task_factory=None, default_timeout=None):
        self.logger = logger or _logger
        self.tag = str(tag)
        self.startup_delay = max(0.0, float(startup_delay))
        self.jitter = max(0.0, float(jitter))
        self.drift_correct = bool(drift_correct)
        self.is_closed = is_closed
        self.task_factory = task_factory
        self.default_timeout = default_timeout
        self._jobs: dict[str, Job] = {}
        self._spawned: set = set()
        self._closed = asyncio.Event()
        self._started = False

    # ---------- 注册与启动 ----------
    def register(self, name, fn, interval, *, immediate=False, timeout=None,
                 max_backoff=None):
        """注册（或取回）一个 job；重复注册同名 job 直接返回已有对象。"""
        job = self._jobs.get(name)
        if job is not None:
            return job
        interval = float(interval)
        if interval <= 0:
            raise ValueError("job %s 的 interval 必须大于 0" % name)
        if not callable(fn):
            raise TypeError("job %s 的 fn 必须可调用" % name)
        if max_backoff is None:
            max_backoff = interval * 8.0
        if max_backoff < interval:
            max_backoff = interval
        if timeout is None:
            timeout = self.default_timeout
        job = Job(name, fn, interval, immediate=immediate, timeout=timeout,
                  max_backoff=max_backoff)
        self._jobs[name] = job
        if self._started and not self._stopping():
            self._ensure_task(job)
        return job

    def start(self):
        """启动所有已注册 job；幂等，可重复调用。"""
        if self._closed.is_set():
            self._closed = asyncio.Event()
        self._started = True
        for job in list(self._jobs.values()):
            self._ensure_task(job)
        return self

    async def wait(self):
        """等待 stop()；供兼容入口挂住生命周期任务。"""
        await self._closed.wait()

    # ---------- 停止与取消 ----------
    async def stop(self, timeout=3.0):
        """取消全部 job 与受管 spawn，等待收尾后返回统计。"""
        self._closed.set()
        self._started = False
        tasks = self._all_tasks()
        for task in tasks:
            try:
                task.cancel()
            except Exception as exc:
                self.logger.debug("[%s] job cancel failed: %r", self.tag, exc)
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=float(timeout))
            except Exception as exc:
                self.logger.debug("[%s] job wait failed: %r", self.tag, exc)
        survivors = [task for task in tasks if not task.done()]
        if survivors:
            self.logger.warning("[%s] job stop: %d 个任务未退出", self.tag,
                                len(survivors))
        for job in self._jobs.values():
            job.running = False
            job.next_run = None
        self._spawned.clear()
        return {"stopped": len(tasks), "survivors": len(survivors)}

    def cancel(self, name):
        """取消单个 job 的调度任务；返回是否真的取消了。"""
        job = self._jobs.get(name)
        if job is None:
            return False
        task = job.task
        if task is None:
            return False
        job.next_run = None
        job.task = None
        task.cancel()
        return True

    def names(self):
        return sorted(self._jobs)

    def task(self, name):
        job = self._jobs.get(name)
        return job.task if job is not None else None

    # ---------- 手动触发与观测 ----------
    async def run_now(self, name):
        """立刻跑一次；正在跑则跳过并记 skipped。"""
        job = self._jobs.get(name)
        if job is None:
            self.logger.warning("[%s] run_now: 未知 job %s", self.tag, name)
            return None
        return await self._execute(job)

    def report(self):
        """可观测快照：每个 job 一行，按名字排序。"""
        return [self._jobs[name].snapshot() for name in self.names()]

    def summary(self):
        rows = self.report()
        return {
            "count": len(rows),
            "running": sum(1 for row in rows if row["running"]),
            "failing": sum(1 for row in rows if row["consecutive_failures"]),
            "jobs": rows,
        }

    # ---------- 受管后台任务（陪伴的 _spawn 走这里） ----------
    @property
    def spawned(self):
        return self._spawned

    def spawn(self, coro, name="background"):
        """受管 spawn：返回 Task，异常统一记日志，不影响其它任务。"""
        task = self._create_task("job:%s" % name, coro)
        if task is None:
            return None
        self._spawned.add(task)
        task.add_done_callback(self._forget)
        return task

    def _create_task(self, name, coro):
        try:
            if self.task_factory is not None:
                return self.task_factory(name, coro)
            return asyncio.create_task(coro, name="%s:%s" % (self.tag, name))
        except RuntimeError:
            try:
                coro.close()
            except Exception as exc:
                self.logger.debug("[%s] spawn close failed: %r", self.tag, exc)
            return None

    def _forget(self, done):
        self._spawned.discard(done)
        if done.cancelled():
            return
        try:
            exc = done.exception()
        except Exception as inner:
            self.logger.debug("[%s] spawn result failed: %r", self.tag, inner)
            return
        if exc is not None:
            self.logger.warning("[%s] 后台任务异常: %s", self.tag, exc,
                                exc_info=exc)

    # ---------- 调度核心 ----------
    def _stopping(self):
        if self._closed.is_set():
            return True
        checker = self.is_closed
        if checker is None:
            return False
        try:
            return bool(checker())
        except Exception as exc:
            self.logger.debug("[%s] is_closed failed: %r", self.tag, exc)
            return False

    def _ensure_task(self, job):
        if job.task is not None and not job.task.done():
            return job.task
        job.next_run = time.monotonic() + self._first_delay(job)
        job.task = self._create_task("job:%s" % job.name, self._loop(job))
        if job.task is not None:
            job.task.add_done_callback(self._forget)
        return job.task

    def _jittered(self, wait, job=None):
        wait = max(0.0, float(wait))
        jitter = self.jitter
        if job is not None and job.jitter is not None:
            jitter = job.jitter
        if jitter and wait > 0:
            wait *= 1.0 + random.uniform(-jitter, jitter)
        return max(0.0, wait)

    def _first_delay(self, job):
        if job.immediate:
            return self._jittered(self.startup_delay, job)
        return self._jittered(job.interval, job)

    def _next_delay(self, job, elapsed):
        interval = job.interval
        if job.consecutive_failures:
            backoff = interval * (2 ** min(job.consecutive_failures, 10))
            interval = min(backoff, job.max_backoff)
        if self.drift_correct:
            floor = min(1.0, interval)
            wait = max(floor, interval - max(0.0, elapsed))
        else:
            wait = interval
        return self._jittered(wait, job)

    async def _loop(self, job):
        delay = self._first_delay(job)
        while not self._stopping():
            job.next_run = time.monotonic() + delay
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                job.next_run = None
                return
            if self._stopping():
                job.next_run = None
                return
            job.next_run = None
            started = time.monotonic()
            try:
                await self._execute(job)
            except asyncio.CancelledError:
                return
            delay = self._next_delay(job, time.monotonic() - started)

    async def _execute(self, job):
        if job.running:
            job.skipped += 1
            return {"name": job.name, "skipped": True, "ok": False,
                    "error": "skipped: still running"}
        job.running = True
        job.runs += 1
        job.last_start = time.monotonic()
        ok = False
        error = ""
        try:
            result = job.fn()
            if inspect.isawaitable(result):
                if job.timeout:
                    await asyncio.wait_for(result, job.timeout)
                else:
                    await result
            ok = True
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except asyncio.TimeoutError:
            job.timeouts += 1
            job.consecutive_failures += 1
            error = "timeout after %ss" % job.timeout
            self.logger.warning("[%s] job %s 超时 %ss，已取消", self.tag,
                                job.name, job.timeout)
        except Exception as exc:
            job.consecutive_failures += 1
            error = "%s: %s" % (type(exc).__name__, exc)
            self.logger.warning("[%s] job %s 异常（已隔离）: %s", self.tag,
                                job.name, exc)
        finally:
            job.running = False
            job.last_end = time.monotonic()
            job.last_error = error
            if ok:
                job.last_ok = True
                job.consecutive_failures = 0
            else:
                job.last_ok = False
        return {"name": job.name, "skipped": False, "ok": ok, "error": error}

    def _all_tasks(self):
        tasks = []
        seen = set()
        for job in self._jobs.values():
            if job.task is not None:
                tasks.append(job.task)
                seen.add(id(job.task))
        for task in list(self._spawned):
            if id(task) not in seen:
                tasks.append(task)
                seen.add(id(task))
        return tasks


__all__ = ["Job", "JobManager"]