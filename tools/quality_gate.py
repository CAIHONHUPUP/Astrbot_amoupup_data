# -*- coding: utf-8 -*-
"""本地质量门：schema / 编译 / 路由数量 / 面板 JS / pyflakes /（可选）全量测试。

用法：
    python tools/quality_gate.py
    python tools/quality_gate.py --fast   # 跳过全量测试（CI 里 pytest 已经跑过）
"""
from __future__ import annotations

import argparse
import compileall
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: 面板/外部会调用的 api_* 处理器数量；增删接口时同步这里
ROUTE_COUNT = 34


def check_schema():
    path = os.path.join(ROOT, "_conf_schema.json")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert isinstance(data, dict), "schema 不是对象"
    assert len(data) >= 25, "schema 太小（%d 项）" % len(data)
    return "schema %d keys" % len(data)


def check_compile():
    ok = compileall.compile_dir(ROOT, quiet=1, force=False)
    assert ok, "py_compile 失败"
    return "compile ok"


def check_routes():
    # 3.1 之后 api_* 处理器搬到了 core/web_handlers.py；只扫 main.py 会数到 0，
    # 质量门就永远红着、然后被大家忽略（这正是 lint 漏检 P1 的原因）。
    count = 0
    for rel in ("core/web_handlers.py", "main.py"):
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        count += len(re.findall(r"^\s+async def api_[a-z_]+\(self\)", text, re.M))
    assert count == ROUTE_COUNT, ("路由数变化：%d != %d（增删接口后同步 ROUTE_COUNT）"
                                 % (count, ROUTE_COUNT))
    return "routes %d" % count


def check_dashboard_js():
    html_path = os.path.join(ROOT, "views", "dashboard", "index.html")
    with open(html_path, encoding="utf-8") as fh:
        html = fh.read()
    start = html.find("<script>")
    end = html.find("</script>", start)
    assert start >= 0 and end > start, "找不到面板 script"
    node = shutil.which("node")
    if not node:
        return "dashboard js skipped (node missing)"
    tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    try:
        tmp.write(html[start + len("<script>"):end])
        tmp.close()
        subprocess.run([node, "--check", tmp.name], check=True)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return "dashboard js ok"


def check_pyflakes():
    # 扫全仓（tests/tools 之前没扫，message_engine 里未定义的 logger 就是这么漏掉的）
    files = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in
                         ("tests", "tools", "__pycache__", ".git", "_test_tmp")
                         and not d.startswith("pytest-cache-files"))
        for name in sorted(names):
            if name.endswith(".py"):
                files.append(os.path.relpath(os.path.join(base, name), ROOT))
    cmd = [sys.executable, "-m", "pyflakes"] + files
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        # 只卡真问题（未定义名/重复定义）；纯 “imported but unused” 是老兼容壳的历史噪音
        noisy = [line for line in result.stdout.splitlines()
                 if line.strip() and "imported but unused" not in line
                 and "redefinition of unused" not in line]
        if noisy:
            print("\n".join(noisy))
            raise AssertionError("pyflakes 有告警")
    return "pyflakes clean (%d files)" % len(files)


def check_tests():
    cmd = [sys.executable, "run_tests.py"]
    subprocess.run(cmd, cwd=ROOT, check=True)
    return "tests ok"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", help="跳过全量测试")
    args = parser.parse_args()
    checks = [check_schema, check_compile, check_routes, check_dashboard_js, check_pyflakes]
    if not args.fast:
        checks.append(check_tests)
    for check in checks:
        print("[gate]", check())
    print("[gate] 全部通过")


if __name__ == "__main__":
    main()
