#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""清理测试留下的 %TEMP%/amo-* 临时目录和文件（换机恢复后也能直接用）。

安全边界：
- 只处理系统临时目录（tempfile.gettempdir()）的“直接子项”，不递归找、不接受自定义根；
- 只认 amo- 前缀，且跳过符号链接；
- 解析真实路径后仍必须是临时目录的直接子项；
- 路径里出现 plugin_data / astrbot/data 一律跳过（双保险）；
- 默认只清 6 小时前的，确认测试全停了再用 --all。
"""
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import time

PREFIX = "amo-"


def _protected(low_path):
    return ("/plugin_data/" in low_path or low_path.endswith("/plugin_data")
            or "/astrbot/data/" in low_path or low_path.endswith("/astrbot/data")
            or "/data/config/" in low_path or low_path.endswith("/data/config"))


def purge(base, max_age_hours, dry_run=False):
    root = os.path.realpath(base)
    cutoff = time.time() - max(0.0, max_age_hours) * 3600
    removed, kept = [], 0
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return removed, kept
    for name in names:
        if not name.startswith(PREFIX) or len(name) <= len(PREFIX):
            continue
        path = os.path.join(root, name)
        try:
            if os.path.islink(path):
                continue
            if hasattr(os.path, "isjunction") and os.path.isjunction(path):
                continue
            real = os.path.realpath(path)
        except OSError:
            continue
        low = real.lower().replace("\\", "/")
        # 只允许临时根的直接子项；符号链接/跨目录解析一律不碰
        if os.path.dirname(real) != root:
            continue
        if _protected(low):
            continue
        try:
            if os.path.getmtime(real) > cutoff:
                kept += 1
                continue
        except OSError:
            continue
        if not dry_run:
            if os.path.isdir(real):
                shutil.rmtree(real, ignore_errors=True)
            else:
                try:
                    os.unlink(real)
                except OSError:
                    continue
        removed.append(real)
    return removed, kept


def main():
    ap = argparse.ArgumentParser(description="清理 %TEMP% 下 amo-* 测试临时文件")
    ap.add_argument("--max-age-hours", type=float, default=6.0,
                    help="只清这个时间以前的（默认 6 小时）")
    ap.add_argument("--all", action="store_true", help="连最近的也清（确认没有测试在跑）")
    ap.add_argument("--dry-run", action="store_true", help="只列出，不删除")
    args = ap.parse_args()
    base = tempfile.gettempdir()
    age = 0.0 if args.all else args.max_age_hours
    removed, kept = purge(base, age, args.dry_run)
    verb = "将清" if args.dry_run else "已清"
    print("%s %d 个 amo-*，保留 %d 个未满 %.1f 小时的" % (verb, len(removed), kept, age))
    for path in removed[:20]:
        print("  " + path)
    if len(removed) > 20:
        print("  ... 其余 %d 个" % (len(removed) - 20))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
