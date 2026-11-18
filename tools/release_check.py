# -*- coding: utf-8 -*-
"""发布前自检（Phase 19）：metadata / main / CHANGELOG / README 版本一致 + 安装包内容。

只读、不打包、不联网：
- 版本四件套逐字比对，不一致直接报错（退出码 1）；
- 给 --zip 时只检查 zip 里是否包含必要文件，不实际打包；
- 不给 --zip 时退化为「目录内必要文件齐全」检查，并明确提示没验安装包。

三插件同一份（sha256 一致）。
用法：
    python tools/release_check.py [--dir <插件目录>] [--zip <安装包.zip>]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(TOOL_DIR)

#: 安装包必须包含的文件（相对插件根目录）
REQUIRED_FILES = ("main.py", "metadata.yaml", "README.md", "CHANGELOG.md")
VERSION_LINE = re.compile(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", re.M)
CODE_VERSION = re.compile(r'PLUGIN_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"')
ANY_VERSION = re.compile(r"([0-9]+\.[0-9]+\.[0-9]+)")


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def first_match(pattern, text):
    match = pattern.search(text)
    return match.group(1) if match else ""


def check_versions(root):
    """四件套版本一致性：metadata / main / CHANGELOG / README。"""
    errors = []
    paths = {name: os.path.join(root, name) for name in REQUIRED_FILES}
    for name, path in paths.items():
        if not os.path.isfile(path):
            errors.append("缺少文件：%s" % name)
    versions = {}
    if not errors:
        versions["metadata"] = first_match(VERSION_LINE, read_text(paths["metadata.yaml"]))
        versions["main"] = first_match(CODE_VERSION, read_text(paths["main.py"]))
        versions["changelog"] = first_match(ANY_VERSION, read_text(paths["CHANGELOG.md"]))
        versions["readme"] = first_match(ANY_VERSION, read_text(paths["README.md"]))
        for name, value in versions.items():
            if not value:
                errors.append("%s 里找不到版本号" % name)
        unique = sorted({value for value in versions.values() if value})
        if len(unique) > 1:
            errors.append("版本不一致：" + "，".join(
                "%s=%s" % (name, versions[name]) for name in sorted(versions)))
    return {"ok": not errors, "versions": versions, "errors": errors}


def check_package(root):
    """没有安装包时：至少检查目录内必要文件齐全。"""
    missing = [name for name in REQUIRED_FILES if not os.path.isfile(os.path.join(root, name))]
    return {"ok": not missing, "missing": missing, "checked": list(REQUIRED_FILES)}


def check_zip(zip_path):
    """只读校验安装包内容，不实际打包。"""
    if not os.path.isfile(zip_path):
        return {"ok": False, "missing": [], "count": 0,
                "errors": ["安装包不存在：%s" % zip_path]}
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = [item.filename.replace("\\", "/").lstrip("./") for item in archive.infolist()]
    except (OSError, zipfile.BadZipFile) as exc:
        return {"ok": False, "missing": [], "count": 0,
                "errors": ["不是合法 zip：%s" % exc]}
    missing = [name for name in REQUIRED_FILES
               if not any(item == name or item.endswith("/" + name) for item in names)]
    errors = []
    if missing:
        errors.append("安装包缺少必要文件：" + "、".join(missing))
    return {"ok": not errors, "missing": missing, "count": len(names), "errors": errors}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="release_check.py", description="发布前自检")
    parser.add_argument("--dir", default=PLUGIN_ROOT)
    parser.add_argument("--zip", default="")
    args = parser.parse_args(argv)
    root = os.path.abspath(args.dir)
    version = check_versions(root)
    package = check_package(root)
    archive = check_zip(args.zip) if args.zip else None
    print("[发版] 目录：%s" % root)
    if version["ok"]:
        print("[发版] 版本一致：%s" % "，".join(
            "%s=%s" % (name, version["versions"][name]) for name in sorted(version["versions"])))
    else:
        for error in version["errors"]:
            print("[发版] 版本错误：%s" % error)
    if package["ok"]:
        print("[发版] 目录必要文件齐全：%s" % "、".join(package["checked"]))
    else:
        print("[发版] 目录缺少必要文件：%s" % "、".join(package["missing"]))
    if archive is None:
        print("[发版] 未提供 --zip，安装包内容未校验（只验了目录）")
    elif archive["ok"]:
        print("[发版] 安装包内容通过（%d 个条目）" % archive["count"])
    else:
        for error in archive["errors"]:
            print("[发版] 安装包错误：%s" % error)
    ok = version["ok"] and package["ok"] and (archive is None or archive["ok"])
    print("[发版] 结论：%s" % ("可以发布" if ok else "不能发布"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
