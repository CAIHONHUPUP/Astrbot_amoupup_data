# -*- coding: utf-8 -*-
"""测试专用：把临时目录固定到仓库内，并绕开 Windows 沙箱的目录 ACL。

DSH 在 Windows 上给「0700 私有目录」加了拒绝 ACE（sandbox SID + DENY），
`tempfile.mkdtemp()` 建出来的目录连自己进程都写不进去；`os.makedirs()` 建的目录
则正常继承父目录权限。这里把 mkdtemp 换成 makedirs 版本，只影响测试进程。
"""
import os
import tempfile
import time as _time

# 测试固定按插件默认时区算，避免 GitHub Actions 的 UTC runner 和本地差 8 小时。
os.environ["TZ"] = "Asia/Shanghai"
try:
    _time.tzset()
except AttributeError:  # Windows 没有 tzset：本地时区本来就是上海
    pass

_BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".test-tmp")
os.makedirs(_BASE, exist_ok=True)
tempfile.tempdir = _BASE


def _mkdtemp(suffix=None, prefix=None, dir=None):
    base = dir or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    for _ in range(100):
        name = (prefix or "tmp") + next(tempfile._get_candidate_names()) + (suffix or "")
        path = os.path.join(base, name)
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            continue
    raise FileExistsError("could not create temp dir")


tempfile.mkdtemp = _mkdtemp
