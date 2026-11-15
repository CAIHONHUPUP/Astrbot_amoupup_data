# -*- coding: utf-8 -*-
"""本地质量门：schema / 编译 / 路由数量 / 面板 JS / pyflakes /（可选）全量测试。

用法：
    python tools/quality_gate.py
    python tools/quality_gate.py --fast   # 跳过全量测试（CI 里 pytest 已经跑过）
"""
from __future__ import annotations

import argparse
import ast
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


# ---- 防长胖预算（2026-11-15）----
# main.py 曾经长到 19k 行；这里把它和单函数长度、静默吞异常都钉住，只许往下走。
MAIN_MAX_LINES = 900
FUNC_WARN_LINES = 250
FUNC_FAIL_LINES = 400
# ---- 代码健康策略（2026-11-15）----
# 1) 静默 except：默认必须带作用域（_note_swallowed / isolated / logger.debug）。
#    清理类代码要静默，必须在 except 行或 pass 行显式写 CLEANUP_MARK。
#    历史欠账按文件给预算（只许往下走）；不在表里的文件必须 0 条未标注静默 ——
#    也就是说「新文件 / 新模块」一律不许再写裸 pass。
# 2) 异步门禁：async 函数里不许有同步阻塞调用（sleep / 网络 / 子进程），
#    必须用 asyncio.to_thread、run_io 之类包起来；极少数确有必要显式写 BLOCK_IO_MARK。
SILENT_BUDGET = 40
CLEANUP_MARK = "# cleanup"
BLOCK_IO_MARK = "# block-io-ok"
SILENT_FILE_BUDGET = {
    "models/memory_db.py": 7,
    "services/maintenance_engine.py": 6,
    "main.py": 5,
    "services/memory_engine.py": 5,
    "services/message_engine.py": 4,
    "services/person_engine.py": 3,
    "core/web_handlers.py": 2,
    "models/dao.py": 2,
    "services/retrieval_engine.py": 2,
    "utils/clock.py": 2,
    "services/backup_engine.py": 1,
    "services/profile_engine.py": 1,
}
_BLOCKING_CALLS = (
    "time.sleep", "sleep",
    "urllib.request.urlopen", "requests.get", "requests.post", "requests.put",
    "requests.delete", "requests.request", "socket.create_connection",
    "subprocess.run", "subprocess.call", "subprocess.check_output", "subprocess.Popen",
    "os.system", "os.popen",
)
_THREAD_WRAPPERS = ("to_thread", "run_in_executor", "run_io", "run_db", "to_thread_run")


def _main_functions():
    import ast
    path = os.path.join(ROOT, "main.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    return [(node.name, node.end_lineno - node.lineno + 1) for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def check_size():
    """防长胖：main.py 行数 + 单函数长度（250 行警告、400 行失败）。"""
    path = os.path.join(ROOT, "main.py")
    with open(path, encoding="utf-8") as fh:
        total = len(fh.read().splitlines())
    funcs = _main_functions()
    problems = []
    if total > MAIN_MAX_LINES:
        problems.append("main.py %d 行 > %d" % (total, MAIN_MAX_LINES))
    for name, size in funcs:
        if size >= FUNC_FAIL_LINES:
            problems.append("%s() %d 行 >= %d" % (name, size, FUNC_FAIL_LINES))
        elif size >= FUNC_WARN_LINES:
            print("[gate][warn] %s() %d 行，已经接近 %d 行上限" % (name, size, FUNC_FAIL_LINES))
    assert not problems, "防长胖预算超了：" + "；".join(problems)
    longest = max((size for _name, size in funcs), default=0)
    return "main.py %d 行，最长函数 %d 行" % (total, longest)


def _silent_excepts():
    """返回 (未标注静默数, 未标注明细, 已标注清理数)。"""
    total = 0
    spots = []
    marked = 0
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".git", "tests", "tools",
                                "_test_tmp", ".test-tmp")]
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                tree = ast.parse(text, filename=path)
            except (SyntaxError, OSError):
                continue
            lines = text.splitlines()
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                body = [n for n in node.body
                        if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                                and isinstance(n.value.value, str))]
                if not (len(body) == 1 and isinstance(body[0], ast.Pass)):
                    continue
                head = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
                tail = lines[body[0].lineno - 1] if body[0].lineno - 1 < len(lines) else ""
                if CLEANUP_MARK in head or CLEANUP_MARK in tail:
                    marked += 1
                    continue
                total += 1
                spots.append("%s:%d" % (os.path.relpath(path, ROOT).replace("\\", "/"),
                                        node.lineno))
    return total, spots, marked


def check_silent_except():
    """静默 except 门禁：新代码必须带作用域；清理类代码显式标注 CLEANUP_MARK。"""
    total, spots, marked = _silent_excepts()
    assert total <= SILENT_BUDGET, (
        "静默 except 超预算：%d > %d；最该改的几处：%s"
        % (total, SILENT_BUDGET, ", ".join(spots[:10]))
    )
    counts = {}
    for spot in spots:
        name = spot.rsplit(":", 1)[0]
        counts[name] = counts.get(name, 0) + 1
    over = []
    for name, count in sorted(counts.items()):
        cap = SILENT_FILE_BUDGET.get(name, 0)
        if count > cap:
            over.append("%s %d>%d" % (name, count, cap))
    assert not over, (
        "新增了没作用域的静默 except（要么记一笔，要么写 %s）：%s"
        % (CLEANUP_MARK, "；".join(over))
    )
    return "silent except %d/%d（%d 个文件，%d 处标注清理）" % (
        total, SILENT_BUDGET, len(counts), marked)


def _blocking_calls():
    """async 函数里的同步阻塞调用（未包 to_thread 的 sleep / 网络 / 子进程）。"""
    problems = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".git", "tests", "tools",
                                "_test_tmp", ".test-tmp")]
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                tree = ast.parse(text, filename=path)
            except (SyntaxError, OSError):
                continue
            lines = text.splitlines()
            parents = {}
            for node in ast.walk(tree):
                for child in ast.iter_child_nodes(node):
                    parents[child] = node
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                try:
                    callee = ast.unparse(node.func)
                except Exception:
                    continue
                if callee not in _BLOCKING_CALLS:
                    continue
                owner = parents.get(node)
                while owner is not None and not isinstance(
                        owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = parents.get(owner)
                if owner is None or not isinstance(owner, ast.AsyncFunctionDef):
                    continue
                wrapped = False
                cursor = parents.get(node)
                while cursor is not None and cursor is not owner:
                    if isinstance(cursor, ast.Call):
                        try:
                            outer = ast.unparse(cursor.func)
                        except Exception:
                            outer = ""
                        if any(word in outer for word in _THREAD_WRAPPERS):
                            wrapped = True
                            break
                    cursor = parents.get(cursor)
                if wrapped:
                    continue
                line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
                if BLOCK_IO_MARK in line:
                    continue
                problems.append("%s:%d %s 在 %s()" % (
                    os.path.relpath(path, ROOT).replace("\\", "/"),
                    node.lineno, callee, owner.name))
    return problems


def check_async_blocking():
    """异步门禁：主链路上的同步阻塞会把事件循环卡住（所有聊天排队）。"""
    problems = _blocking_calls()
    assert not problems, (
        "异步函数里有同步阻塞调用：%s；请用 asyncio.to_thread / run_io 包起来，"
        "确有必要则写 %s" % ("；".join(problems[:8]), BLOCK_IO_MARK)
    )
    return "async blocking clean"


def clean_pycache():
    """旧 __pycache__ 会让 Python 复用「mtime+size 相同、内容不同」的 pyc：
    热重载后磁盘是新代码、进程里还是旧的（2026-11-15 实测踩到：main.py 已是 1.0.6，
    日志里模块自己打印的还是 1.0.4）。质量门先清一遍，别让门禁和线上各看一份。"""
    removed = 0
    for base, dirs, _names in os.walk(ROOT):
        for name in list(dirs):
            if name == "__pycache__":
                shutil.rmtree(os.path.join(base, name), ignore_errors=True)
                dirs.remove(name)
                removed += 1
    return removed


#: 单函数预算（2026-11-15 实测；只许往下）。不在表里的函数超过 250 行要拆，
#: 表里的按记录值封顶 —— 想加逻辑请拆函数，而不是把老函数继续加长。
FUNC_BUDGET = {
}


def _all_functions():
    rows = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "tests", "tools",
                                                "_test_tmp", ".test-tmp")]
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read(), filename=path)
            except (SyntaxError, OSError):
                continue
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    size = (node.end_lineno or node.lineno) - node.lineno + 1
                    rows.append((rel, node.name, size))
    return rows


def check_function_sizes():
    """全仓单函数长度：超预算就红（main.py 由 check_size 另管）。"""
    problems = []
    longest = ("", "", 0)
    for rel, name, size in _all_functions():
        key = "%s::%s" % (rel, name)
        if size > longest[2]:
            longest = (rel, name, size)
        if key in FUNC_BUDGET:
            if size > FUNC_BUDGET[key]:
                problems.append("%s %d > %d（登记在册，只许拆不许加）" % (key, size, FUNC_BUDGET[key]))
            continue
        if size >= FUNC_FAIL_LINES:
            problems.append("%s %d >= %d" % (key, size, FUNC_FAIL_LINES))
        elif size >= FUNC_WARN_LINES:
            print("[gate][warn] %s %d 行，考虑拆一下" % (key, size))
    assert not problems, "函数太长：%s" % "；".join(problems[:6])
    return "functions ok（最长 %s::%s %d 行）" % longest


def check_tests():

    cmd = [sys.executable, "run_tests.py"]
    subprocess.run(cmd, cwd=ROOT, check=True)
    return "tests ok"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", help="跳过全量测试")
    args = parser.parse_args()
    print("[gate] clean_pycache", clean_pycache())
    checks = [check_schema, check_compile, check_routes, check_dashboard_js, check_pyflakes,
              check_size, check_function_sizes, check_silent_except, check_async_blocking]
    if not args.fast:
        checks.append(check_tests)
    for check in checks:
        print("[gate]", check())
    print("[gate] 全部通过")


if __name__ == "__main__":
    main()
