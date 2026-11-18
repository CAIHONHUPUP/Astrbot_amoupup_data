# -*- coding: utf-8 -*-
"""在任意工作目录下跑 mypy：把插件目录当成一个包，相对导入才解得开。

CI 里直接 `python -m mypy` 时，插件根目录就是仓库根，mypy 认不出它的包名，
`from .services.memory_engine import MemoryEngine` 会报 "No parent module"。这里把父目录放进
MYPYPATH、按目录检查，任何 cwd 下都能跑。

用法：
    python tools/mypy_check.py
    python tools/mypy_check.py services/memory_engine.py
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT = os.path.dirname(ROOT)
EXCLUDE = r"(?x)(/tests/|/tools/|/work/|/__pycache__/)"


def main(argv):
    env = dict(os.environ)
    old = env.get("MYPYPATH", "")
    env["MYPYPATH"] = PARENT + (os.pathsep + old if old else "")
    targets = [os.path.join(ROOT, arg) if arg.endswith(".py") else arg for arg in argv]
    cmd = [sys.executable, "-m", "mypy",
           "--config-file", os.path.join(ROOT, "mypy.ini"),
           # mypy.ini 的策略是「注解增量覆盖」：不强制检查未注解函数体；带
           # --check-untyped-defs 会把 work/ 模拟脚本的未注解体算成噪音（2026-11-17 统一口径）。
           "--explicit-package-bases", "--no-incremental",
           "--exclude", EXCLUDE]
    cmd += targets or [ROOT]
    return subprocess.call(cmd, env=env, cwd=PARENT)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
