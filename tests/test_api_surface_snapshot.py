# -*- coding: utf-8 -*-
"""能记对外门面快照：MemoryAPI 的方法名与参数一旦变动，必须显式更新快照。

守护/陪伴插件只认 `plugin.mem`（见 core/api.py 顶部契约），这里把它的公开表面钉住：
删方法、改参数名都会让快照对不上，从而在 CI/本地立刻报出来，而不是等线上「属性不存在」。

更新快照：
    python tests/test_api_surface_snapshot.py --update
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(ROOT, "tests", "contracts", "memory_api_surface.json")
API_FILE = os.path.join(ROOT, "core", "api.py")
CLASS_NAME = "MemoryAPI"


def surface():
    """{方法名: [参数名...]}（跳过 self/cls）。"""
    with io.open(API_FILE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=API_FILE)
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != CLASS_NAME:
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name.startswith("_"):
                continue
            args = [a.arg for a in list(item.args.args) + list(item.args.kwonlyargs)
                    if a.arg not in ("self", "cls")]
            out[item.name] = args
    return out


def load_snapshot():
    with io.open(SNAPSHOT, encoding="utf-8") as fh:
        return json.load(fh)


def write_snapshot(data):
    os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
    with io.open(SNAPSHOT, "w", encoding="utf-8", newline="") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def declared_count():
    """core/api.py 顶部写的「N 个方法」和实际数量是否一致。"""
    with io.open(API_FILE, encoding="utf-8") as fh:
        text = fh.read()
    import re
    match = re.search(r"(\d+)\s*个方法", text)
    return int(match.group(1)) if match else None


class ApiSurfaceTests(unittest.TestCase):
    def test_surface_matches_snapshot(self):
        current = surface()
        snapshot = load_snapshot()
        missing = sorted(set(snapshot) - set(current))
        added = sorted(set(current) - set(snapshot))
        changed = sorted(name for name in set(current) & set(snapshot)
                         if list(current[name]) != list(snapshot[name]))
        detail = []
        if missing:
            detail.append("被删/改名：%s（守护/陪伴可能直接 AttributeError）" % ", ".join(missing))
        if added:
            detail.append("新增：%s（记得同步快照）" % ", ".join(added))
        if changed:
            detail.append("参数变了：%s" % ", ".join(
                "%s %s->%s" % (n, snapshot[n], current[n]) for n in changed))
        self.assertFalse(detail, "MemoryAPI 表面变了：\n  " + "\n  ".join(detail)
                         + "\n若是有意改动，跑 python tests/test_api_surface_snapshot.py --update")

    def test_declared_method_count(self):
        count = declared_count()
        self.assertIsNotNone(count, "core/api.py 顶部缺「N 个方法」说明")
        self.assertEqual(count, len(surface()), "接口数量和顶部契约说明对不上")


if __name__ == "__main__":
    if "--update" in sys.argv:
        write_snapshot(surface())
        print("snapshot written:", SNAPSHOT)
    else:
        unittest.main()
