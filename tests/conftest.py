# -*- coding: utf-8 -*-
"""pytest 会话级测试环境（Phase 18，三插件同一份）。

- tempfile.tempdir 指向一次性 tempfile.mkdtemp() 根目录，进程退出整棵删除；
- DSH 沙箱会给 0700 目录加 DENY ACE，所以 mkdtemp 换成 os.makedirs(0o777) 等价实现；
- 会话开始清掉历史遗留的 .test-tmp/_test_tmp，结束时断言它们没有再出现；
- 固定 TZ=Asia/Shanghai，避免 CI 的 UTC runner 与本地差 8 小时。
"""
import atexit
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))

# 单文件/单类运行也要先装 astrbot 桩：以前只有 test_data_p2 在 import 时 install()，
# 于是 `pytest tests/test_recall_fixes.py` 会在收集阶段 ModuleNotFoundError: astrbot，
# 而全量跑因为文件顺序碰巧能过 —— 典型的测试顺序依赖，这里统一在收集前装好。
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)
try:
    import astrbot_stub

    astrbot_stub.install()
except ImportError:  # pragma: no cover - stub 缺失时退回旧行为
    pass

import time as _time
if hasattr(_time, "tzset"):
    # 只有 Unix 的 tzset 才真的能换时区。Windows 上「只设 TZ 不 tzset」会让 C 运行库
    # 把 IANA 名字当非法值回落 UTC，datetime.now() 直接差 8 小时（2026-11-18 本地
    # 9 个测试因此红）；Windows 本机时区本来就是上海，不碰 TZ 最安全。
    os.environ["TZ"] = "Asia/Shanghai"
    _time.tzset()

_ORIG_MKDTEMP = tempfile.mkdtemp
_TEMP_ROOT = _ORIG_MKDTEMP(prefix="amo-tests-")
_SYSTEM_TEMP = tempfile.gettempdir()
_SESSION_DIRS = []
try:
    os.chmod(_TEMP_ROOT, 0o777)
except OSError:
    pass
tempfile.tempdir = _TEMP_ROOT


def _mkdtemp(suffix=None, prefix=None, dir=None):
    """os.makedirs 版本：绕开沙箱对 mkdtemp 0700 目录的 DENY ACE。"""
    base = dir or tempfile.gettempdir()
    suffix = suffix or ""
    prefix = prefix or "tmp"
    os.makedirs(base, mode=0o777, exist_ok=True)
    for _ in range(200):
        path = os.path.join(base, prefix + next(tempfile._get_candidate_names()) + suffix)
        try:
            os.makedirs(path, mode=0o777, exist_ok=False)
        except FileExistsError:
            continue
        _SESSION_DIRS.append(path)
        return path
    raise FileExistsError("could not create a temporary directory")


tempfile.mkdtemp = _mkdtemp
setattr(tempfile.TemporaryDirectory, "_mkdtemp", staticmethod(_mkdtemp))


def _legacy_dirs():
    return [os.path.join(ROOT, name) for name in (".test-tmp", "_test_tmp")]


def cleanup_session_temp_root():
    while _SESSION_DIRS:
        shutil.rmtree(_SESSION_DIRS.pop(), ignore_errors=True)
    shutil.rmtree(_TEMP_ROOT, ignore_errors=True)


def _purge_stale_temp_dirs(max_age_hours=6):
    """清掉以前跑测试留下的 %TEMP% 下 amo-* 目录；只动 6 小时前的，避免误删正在跑的。"""
    cutoff = _time.time() - max_age_hours * 3600
    try:
        names = os.listdir(_SYSTEM_TEMP)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if not name.startswith("amo-"):
            continue
        path = os.path.join(_SYSTEM_TEMP, name)
        try:
            if os.path.islink(path):
                continue
            if os.path.getmtime(path) > cutoff:
                continue
        except OSError:
            continue
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.unlink(path)
            except OSError:
                continue
        removed += 1
    return removed


_purge_stale_temp_dirs()


for _legacy in _legacy_dirs():
    shutil.rmtree(_legacy, ignore_errors=True)

atexit.register(cleanup_session_temp_root)


try:
    import pytest
except ImportError:  # gate 用 unittest，没有 pytest 也不影响
    pytest = None  # type: ignore[assignment]  # 没装 pytest 时占位


if pytest is not None:
    @pytest.fixture(scope="session", autouse=True)
    def _amo_temp_guard():
        yield
        leftover = [path for path in _legacy_dirs() if os.path.exists(path)]
        cleanup_session_temp_root()
        if leftover:
            raise AssertionError("测试跑完还残留旧临时目录：%s" % leftover)

    @pytest.fixture
    def fixed_clock():
        from fixtures import FixedClock
        with FixedClock() as frozen:
            yield frozen

    @pytest.fixture
    def temp_db():
        from support import make_temp_dir
        root = make_temp_dir("amo-p2-db-")
        try:
            yield os.path.join(root, "amo-test.db")
        finally:
            shutil.rmtree(root, ignore_errors=True)

    @pytest.fixture
    def temp_dir():
        from support import make_temp_dir
        root = make_temp_dir("amo-p2-dir-")
        try:
            yield root
        finally:
            shutil.rmtree(root, ignore_errors=True)
