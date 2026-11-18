# -*- coding: utf-8 -*-
"""发布回滚工具（Phase 19）：manifest / verify / plan / apply，默认只做 dry-run。

安全边界（任何一条不满足都只拒绝，不做写入）：
- 所有目录必须落在「插件父目录」（默认 = 本脚本所在插件目录的上一级）之内；
- apply 必须先通过清单校验（sha256 全量比对），清单不匹配直接拒绝；
- apply 先把当前目标目录整体备份成 <target>.rollback-<时间戳>，再覆盖；
- 覆盖后抽样 py_compile，编译不过报失败并给出备份目录，不假装成功。

纯标准库、无第三方依赖；三插件同一份（sha256 一致）。
用法：
    python tools/rollback.py manifest --dir <备份目录> --out <备份目录>/manifest.json
    python tools/rollback.py verify   --dir <备份目录>
    python tools/rollback.py plan     --backup <备份目录> --target <插件目录>
    python tools/rollback.py apply    --backup <备份目录> --target <插件目录> [--confirm <token>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(TOOL_DIR)
DEFAULT_BASE = os.path.dirname(PLUGIN_ROOT)

MANIFEST_NAME = "manifest.json"
SKIP_DIRS = ("__pycache__", ".git", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", ".coverage")
SKIP_NAMES = ("manifest.json", ".rollback_manifest.json", ".DS_Store")
SAMPLE_COMPILE = 20


class Refused(Exception):
    """越界 / 清单不匹配 / 参数不对：只拒绝，不做任何写操作。"""


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk_files(root):
    """目录下所有普通文件（相对路径，/ 分隔，排序）。"""
    found = []
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            if name in SKIP_NAMES or name.endswith(".pyc"):
                continue
            path = os.path.join(base, name)
            if os.path.isfile(path):
                found.append(os.path.relpath(path, root).replace("\\", "/"))
    return sorted(found)


def manifest(root):
    """给目录生成 sha256 清单：{root, count, digest, files:{相对路径: sha256}}。"""
    files = {}
    for rel in walk_files(root):
        files[rel] = _sha256_file(os.path.join(root, rel))
    payload = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {"root": os.path.abspath(root), "count": len(files),
            "digest": digest, "files": files}


def write_manifest(root, out_path):
    data = manifest(root)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return data


def load_manifest(path):
    if not os.path.isfile(path):
        raise Refused("清单不存在：%s（先跑 manifest 生成清单）" % path)
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise Refused("清单格式不对：%s" % path)
    return data


def resolve_inside(path, base):
    """把 path 规范成绝对路径，并确认它在 base 之内（不能等于 base）。"""
    target = os.path.abspath(path)
    base_abs = os.path.abspath(base)
    if os.path.normcase(target) == os.path.normcase(base_abs):
        raise Refused("不能直接操作插件父目录本身：%s" % target)
    try:
        common = os.path.commonpath([target, base_abs])
    except ValueError:
        raise Refused("路径不在允许范围内：%s（基准 %s）" % (target, base_abs))
    if os.path.normcase(common) != os.path.normcase(base_abs):
        raise Refused("路径越界：%s 不在插件父目录 %s 之内" % (target, base_abs))
    return target


def diff_files(source, destination):
    """source 相对 destination 的差异（apply 视角：source=备份，destination=当前）。"""
    added = sorted(set(source) - set(destination))
    extra = sorted(set(destination) - set(source))
    changed = sorted(rel for rel in set(source) & set(destination)
                     if source[rel] != destination[rel])
    return {"added": added, "extra": extra, "changed": changed}


def verify(backup, manifest_path, base):
    """清单校验：备份目录当前内容必须和 manifest 完全一致。"""
    backup = resolve_inside(backup, base)
    saved = load_manifest(os.path.abspath(manifest_path))
    current = manifest(backup)
    diff = diff_files(saved["files"], current["files"])
    ok = saved.get("digest") == current.get("digest")
    return {"ok": ok, "backup": backup, "count": current["count"],
            "saved_digest": str(saved.get("digest") or ""),
            "current_digest": current["digest"], "diff": diff}


def plan(backup, target, base):
    """dry-run：只读对比备份与当前目录，指出会新增/覆盖/保留哪些文件。"""
    backup = resolve_inside(backup, base)
    target = resolve_inside(target, base)
    if os.path.normcase(backup) == os.path.normcase(target):
        raise Refused("备份目录与目标目录不能是同一个：%s" % target)
    if not os.path.isdir(backup):
        raise Refused("备份目录不存在：%s" % backup)
    if not os.path.isdir(target):
        raise Refused("目标目录不存在：%s" % target)
    source = manifest(backup)
    destination = manifest(target)
    diff = diff_files(source["files"], destination["files"])
    return {"ok": True, "backup": backup, "target": target, "dry_run": True,
            "token": source["digest"][:12], "source_count": source["count"],
            "target_count": destination["count"], "diff": diff,
            "add": len(diff["added"]), "overwrite": len(diff["changed"]),
            "keep_extra": len(diff["extra"])}


def _compile_sample(root, files):
    targets = [rel for rel in files if rel.endswith(".py")][:SAMPLE_COMPILE]
    if not targets:
        return {"ok": True, "checked": 0, "detail": "没有需要编译的 .py"}
    command = [sys.executable, "-m", "py_compile"] + [os.path.join(root, rel) for rel in targets]
    result = subprocess.run(command, capture_output=True, text=True)
    detail = (result.stderr or result.stdout or "").strip()[-400:]
    return {"ok": result.returncode == 0, "checked": len(targets), "detail": detail}


def apply(backup, target, confirm, manifest_path, base):
    """恢复备份：校验清单 -> 备份当前 -> 覆盖 -> py_compile 抽样。默认 dry-run。"""
    backup = resolve_inside(backup, base)
    target = resolve_inside(target, base)
    if os.path.normcase(backup) == os.path.normcase(target):
        raise Refused("备份目录与目标目录不能是同一个：%s" % target)
    if not os.path.isdir(backup) or not os.path.isdir(target):
        raise Refused("备份或目标目录不存在")
    checked = verify(backup, manifest_path, base)
    token = checked["saved_digest"][:12]
    if not confirm:
        return {"ok": bool(checked["ok"]), "dry_run": True, "token": token,
                "verify": checked, "plan": plan(backup, target, base)}
    if confirm != token:
        raise Refused("确认串不对：应传 %s（先不传 --confirm 看 dry-run）" % token)
    if not checked["ok"]:
        raise Refused("备份清单校验失败，拒绝覆盖：%s" % checked["diff"])
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shelter = target + ".rollback-" + stamp
    serial = 1
    while os.path.exists(shelter):
        serial += 1
        shelter = target + ".rollback-" + stamp + "-" + str(serial)
    shutil.copytree(target, shelter, ignore=shutil.ignore_patterns(*SKIP_DIRS))
    saved = load_manifest(os.path.abspath(manifest_path))
    copied = []
    for rel in sorted(saved["files"]):
        source = os.path.join(backup, rel)
        destination = os.path.join(target, rel)
        parent = os.path.dirname(destination)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(rel)
    compiled = _compile_sample(target, copied)
    return {"ok": compiled["ok"], "dry_run": False, "token": token,
            "shelter": shelter, "copied": len(copied), "compile": compiled}


def _print_manifest(data):
    print("[清单] 目录：%s" % data["root"])
    print("       文件：%d 个，总指纹：%s" % (data["count"], data["digest"][:12]))


def _cmd_manifest(args):
    root = resolve_inside(args.dir, args.root)
    out = os.path.abspath(args.out or os.path.join(root, MANIFEST_NAME))
    resolve_inside(out, args.root)
    data = write_manifest(root, out)
    _print_manifest(data)
    print("       清单已写入：%s" % out)
    return 0


def _cmd_verify(args):
    result = verify(args.dir, args.manifest or os.path.join(args.dir, MANIFEST_NAME), args.root)
    if result["ok"]:
        print("[校验] 通过：%s（%d 个文件，指纹 %s）"
              % (result["backup"], result["count"], result["current_digest"][:12]))
        return 0
    print("[校验] 失败：备份内容和清单不一致")
    print("       新增：%s" % (result["diff"]["added"][:6] or "无"))
    print("       缺失：%s" % (result["diff"]["extra"][:6] or "无"))
    print("       变化：%s" % (result["diff"]["changed"][:6] or "无"))
    return 1


def _cmd_plan(args):
    result = plan(args.backup, args.target, args.root)
    print("[计划] dry-run（不写任何文件）")
    print("       备份：%s（%d 个文件）" % (result["backup"], result["source_count"]))
    print("       目标：%s（%d 个文件）" % (result["target"], result["target_count"]))
    print("       将新增 %d、覆盖 %d、目标多出但保留 %d"
          % (result["add"], result["overwrite"], result["keep_extra"]))
    print("       确认串：%s" % result["token"])
    return 0


def _cmd_apply(args):
    manifest_path = args.manifest or os.path.join(args.backup, MANIFEST_NAME)
    result = apply(args.backup, args.target, args.confirm, manifest_path, args.root)
    if result.get("dry_run"):
        print("[恢复] dry-run（未确认，不动任何文件）")
        print("       清单校验：%s" % ("通过" if result["verify"]["ok"] else "失败"))
        print("       将新增 %d、覆盖 %d" % (result["plan"]["add"], result["plan"]["overwrite"]))
        print("       确认串：%s（通过 --confirm 传入才会真正执行）" % result["token"])
        return 0 if result["ok"] else 1
    print("[恢复] 已覆盖 %d 个文件；当前目录已备份到：%s" % (result["copied"], result["shelter"]))
    compile_info = result["compile"]
    print("[恢复] py_compile 抽样 %d 个文件：%s"
          % (compile_info["checked"], "通过" if compile_info["ok"] else "失败"))
    if not compile_info["ok"]:
        print("       %s" % compile_info["detail"])
        print("       可用上面的备份目录手工回退")
        return 1
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="rollback.py", description="发布回滚工具（默认 dry-run）")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("manifest", help="给目录生成 sha256 清单")
    one.add_argument("--dir", required=True)
    one.add_argument("--out", default="")
    one.add_argument("--root", default=DEFAULT_BASE)
    two = sub.add_parser("verify", help="校验目录与清单是否一致")
    two.add_argument("--dir", required=True)
    two.add_argument("--manifest", default="")
    two.add_argument("--root", default=DEFAULT_BASE)
    three = sub.add_parser("plan", help="dry-run 对比备份与当前目录")
    three.add_argument("--backup", required=True)
    three.add_argument("--target", required=True)
    three.add_argument("--root", default=DEFAULT_BASE)
    four = sub.add_parser("apply", help="恢复备份（先校验清单，再备份当前）")
    four.add_argument("--backup", required=True)
    four.add_argument("--target", required=True)
    four.add_argument("--confirm", default="")
    four.add_argument("--manifest", default="")
    four.add_argument("--root", default=DEFAULT_BASE)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    handlers = {"manifest": _cmd_manifest, "verify": _cmd_verify,
                "plan": _cmd_plan, "apply": _cmd_apply}
    try:
        return handlers[args.command](args)
    except Refused as exc:
        print("[拒绝] %s" % exc)
        return 2
    except OSError as exc:
        print("[失败] 文件操作失败：%s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
