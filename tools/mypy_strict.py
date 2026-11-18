# -*- coding: utf-8 -*-
"""严格 mypy 棘轮（--check-untyped-defs，排除 tests/tools/work）。

基线冻结在 tools/mypy_strict_baseline.json：只许降、不许升；低于基线时打印
「可下调基线」，加 --update 把当前值写回基线。quality_gate 会跑它。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARENT = os.path.dirname(ROOT)
BASELINE = os.path.join(ROOT, "tools", "mypy_strict_baseline.json")
EXCLUDE = r"(?x)(/tests/|/tools/|/work/)"
FOUND = re.compile(r"Found (\d+) errors? in (\d+) files? \(checked (\d+) source files?\)")
CLEAN = re.compile(r"Success: no issues found in (\d+) source files?")


def parse(output: str):
    match = FOUND.search(output)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    match = CLEAN.search(output)
    if match:
        return 0, 0, int(match.group(1))
    return None


def classify(current: int, baseline: int) -> str:
    if current > baseline:
        return "over"
    if current < baseline:
        return "lower"
    return "ok"


def run_mypy() -> str:
    env = dict(os.environ)
    env["MYPYPATH"] = PARENT + (os.pathsep + env["MYPYPATH"] if env.get("MYPYPATH") else "")
    cmd = [sys.executable, "-m", "mypy",
           "--config-file", os.path.join(ROOT, "mypy.ini"),
           "--explicit-package-bases", "--no-incremental", "--check-untyped-defs",
           "--exclude", EXCLUDE, ROOT]
    proc = subprocess.run(cmd, cwd=PARENT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    return (proc.stdout or "") + (proc.stderr or "")


def load_baseline():
    try:
        with open(BASELINE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"errors": 0, "files": 0, "checked": 0}


def main(argv) -> int:
    try:  # 输出统一 UTF-8，父进程 gate 按 UTF-8 解码时不会出现 U+FFFD
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    output = run_mypy()
    parsed = parse(output)
    if parsed is None:
        print("STRICT MYPY: 无法解析输出")
        print(output[-1200:])
        return 2
    errors, files, checked = parsed
    baseline = load_baseline()
    if "--update" in argv:
        with open(BASELINE, "w", encoding="utf-8") as fh:
            json.dump({"errors": errors, "files": files, "checked": checked,
                       "updated": "2026-11-18",
                       "note": "严格 mypy 棘轮基线（--check-untyped-defs，排除 tests/tools/work）"},
                      fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        print("baseline updated:", BASELINE, errors)
        return 0
    state = classify(errors, int(baseline.get("errors") or 0))
    print("strict mypy: %d errors / %d files / %d checked (baseline %d)" % (
        errors, files, checked, int(baseline.get("errors") or 0)))
    if state == "over":
        print("FAIL: 严格 mypy 超过基线（只降不升）")
        return 1
    if state == "lower":
        print("可下调基线：%d -> %d（加 --update 写回）" % (int(baseline.get("errors") or 0), errors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
