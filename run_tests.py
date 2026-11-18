# -*- coding: utf-8 -*-
"""测试入口：临时目录用一次性 mkdtemp 根，跑完删掉，不在仓库留 _test_tmp。"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="amo-run-tests-")
tempfile.tempdir = TMP
os.environ["AMO_DATA_TEST_TMP"] = TMP
sys.path.insert(0, BASE)
atexit.register(lambda: shutil.rmtree(TMP, ignore_errors=True))


def _mkdtemp(suffix="", prefix="tmp", dir=None):
    """mkdtemp 建出来的 0700 目录在本机沙箱里写不进去，换成普通 os.makedirs。"""
    base = dir or tempfile.gettempdir()
    path = os.path.join(base, str(prefix) + uuid.uuid4().hex[:10] + str(suffix))
    os.makedirs(path, mode=0o777, exist_ok=False)
    return path


tempfile.mkdtemp = _mkdtemp

if __name__ == "__main__":
    TESTS = os.path.join(BASE, "tests")
    suite = unittest.defaultTestLoader.discover(TESTS, pattern="test_*.py", top_level_dir=TESTS)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
