# -*- coding: utf-8 -*-
"""services/backup_engine.py —— 快照导出/合并导入、整库备份与恢复。

语义沿用 3.0：导出不含聊天记录；导入按内容去重；恢复=把备份文件当快照合并，不覆盖现有数据。
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List

try:
    from ..utils.text_process import _bot_filter, _now, logger
except ImportError:  # 顶层导入（测试/脚本）
    from utils.text_process import _bot_filter, _now, logger


class BackupEngine:
    _EXPORT_TABLES = ("memories", "profiles", "relations", "persons",
                      "person_links", "identity_alias")

    def __init__(self, db, data_dir, settings=None):
        self.db = db
        self.data_dir = str(data_dir)
        self.settings = settings if settings is not None else {}
        self.plugin = None

    @property
    def conn(self):
        return self.db.conn

    @property
    def lock(self):
        return self.db.lock

    def backup_dir(self) -> str:
        return os.path.join(self.data_dir, "backups")

    # ---------- 快照 ----------
    def _snapshot_from_conn(self, conn, bot_id: str = "") -> Dict[str, Any]:
        where, params = _bot_filter(bot_id)
        out: Dict[str, Any] = {
            "format": "astr_amo_data", "version": 1,
            "exported_at": _now(), "bot_id": str(bot_id or ""),
        }
        for table in self._EXPORT_TABLES:
            try:
                rows = conn.execute(
                    "SELECT * FROM %s WHERE 1=1%s" % (table, where), params
                ).fetchall()
            except Exception:
                rows = []
            out[table] = [dict(row) for row in rows]
        return out

    def export_snapshot(self, bot_id: str = "") -> Dict[str, Any]:
        """导出画像 / 记忆 / 关系 / 人物 / 身份别名（不含聊天记录）。"""
        with self.lock:
            snapshot = self._snapshot_from_conn(self.conn, bot_id)
        snapshot["counts"] = {k: len(snapshot[k]) for k in self._EXPORT_TABLES}
        return snapshot

    def load_snapshot_file(self, db_path: str, bot_id: str = "") -> Dict[str, Any]:
        """从一个备份数据库文件导出同样的快照。"""
        path = str(db_path)
        if not os.path.isfile(path):
            return {}
        conn = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
        try:
            conn.row_factory = sqlite3.Row
            snapshot = self._snapshot_from_conn(conn, bot_id)
        finally:
            conn.close()
        snapshot["counts"] = {k: len(snapshot.get(k) or []) for k in self._EXPORT_TABLES}
        return snapshot

    def import_snapshot(self, data: Dict[str, Any]) -> Dict[str, int]:
        """把快照合并进当前库：记忆/画像/关系按内容去重，人物按身份别名合并。"""
        if not isinstance(data, dict):
            return {"memories": 0, "profiles": 0, "relations": 0, "links": 0, "aliases": 0}
        counts = {"memories": 0, "profiles": 0, "relations": 0, "links": 0,
                  "aliases": 0, "skipped": 0, "failed": 0}
        default_bot = str(data.get("bot_id") or "") or ""
        p = self.plugin
        for item in data.get("memories") or []:
            try:
                bot = str(item.get("bot_id") or default_bot)
                uid = str(item.get("uid") or "")
                content = str(item.get("content") or "")
                if not bot or not uid or not content:
                    continue
                res = (getattr(p, "remember", None) or p.memory.remember)(
                    bot, uid, content,
                    kind=str(item.get("kind") or "fact"),
                    importance=item.get("importance", 0.5),
                    scope=str(item.get("scope") or "private"),
                    group_id=str(item.get("group_id") or ""),
                    idem_key=str(item.get("idem_key") or ""),
                    created_at=item.get("created_at"),
                    last_hit_at=item.get("last_hit_at"),
                    hit_count=item.get("hit_count", 0),
                    keep_existing_content=True,
                )
                if not res.get("ok"):
                    continue
                if res.get("duplicate"):
                    # 已经存在（内容指纹相同）就跳过，重复导入不会算成新增
                    counts["skipped"] += 1
                    continue
                counts["memories"] += 1
                mid = int(res.get("memory_id") or 0)
                status = str(item.get("status") or "")
                if mid and status in ("superseded", "archived", "merged"):
                    with self.lock:
                        self.conn.execute(
                            "UPDATE memories SET status=?, superseded_by=? WHERE id=?",
                            (status, int(item.get("superseded_by") or 0), mid),
                        )
                        self.conn.commit()
            except Exception as exc:
                # 以前这里静默 continue：二次导入时 74 条无声消失、计数还显示成功
                counts["failed"] += 1
                try:
                    logger.warning("[amo_data] 导入记忆失败: %s" % exc)
                except Exception:
                    pass
                continue
        for item in data.get("profiles") or []:
            try:
                bot = str(item.get("bot_id") or default_bot)
                uid = str(item.get("uid") or "")
                if not bot or not uid:
                    continue
                current = p.profiles.get_profile(bot, uid)
                try:
                    remote_at = int(item.get("updated_at") or 0)
                except (TypeError, ValueError):
                    remote_at = 0
                if current and remote_at and remote_at < int(current.get("updated_at") or 0):
                    continue
                (getattr(p, "save_profile", None) or p.profiles.save_profile)(
                    bot, uid,
                    name=item.get("name"), alias=item.get("alias"),
                    relation=item.get("relation"), remark=item.get("remark"),
                )
                counts["profiles"] += 1
            except Exception:
                continue
        for item in data.get("relations") or []:
            try:
                if (getattr(p, "save_relation", None) or p.persons.save_relation)(
                    str(item.get("bot_id") or default_bot),
                    str(item.get("a_uid") or ""), str(item.get("b_uid") or ""),
                    str(item.get("relation") or "朋友"), str(item.get("note") or ""),
                ):
                    counts["relations"] += 1
            except Exception:
                continue
        for item in data.get("person_links") or []:
            try:
                bot = str(item.get("bot_id") or default_bot)
                platform, uid = str(item.get("platform") or ""), str(item.get("uid") or "")
                if not bot or not uid:
                    continue
                (getattr(p, "resolve_person", None) or p.persons.resolve_person)(
                    bot, platform, uid, str(item.get("name") or ""))
                counts["links"] += 1
            except Exception:
                continue
        for item in data.get("identity_alias") or []:
            try:
                if (getattr(p, "set_identity_alias", None) or p.persons.set_identity_alias)(
                    str(item.get("bot_id") or default_bot),
                    str(item.get("platform") or ""), str(item.get("uid") or ""),
                    str(item.get("alias_platform") or ""), str(item.get("alias_uid") or ""),
                ):
                    counts["aliases"] += 1
            except Exception:
                continue
        return counts

    # ---------- 整库备份 ----------
    def backup_database(self, dest_dir: str, keep: int = 7) -> str:
        """把当前库拷贝到指定目录（老接口，测试/脚本用）。"""
        dest_dir = str(dest_dir)
        os.makedirs(dest_dir, exist_ok=True)
        import time as _time
        stem = "amo_data-%s" % _time.strftime("%Y%m%d-%H%M%S", _time.localtime())
        name = stem + ".db"
        index = 1
        while os.path.exists(os.path.join(dest_dir, name)):
            name = "%s-%d.db" % (stem, index)
            index += 1
        dest = os.path.join(dest_dir, name)
        with self.lock:
            target = sqlite3.connect(dest)
            try:
                self.conn.backup(target)
            finally:
                target.close()
        self.prune_backups(dest_dir, keep)
        return dest

    def backup_now(self, keep: int = 7) -> str:
        """把当前 SQLite 拷贝一份到 backups/，并只保留最近 keep 份。"""
        dest_dir = self.backup_dir()
        os.makedirs(dest_dir, exist_ok=True)
        stem = "amo_data-%s" % _now_str()
        name = stem + ".db"
        index = 1
        while os.path.exists(os.path.join(dest_dir, name)):
            name = "%s-%d.db" % (stem, index)
            index += 1
        dest = os.path.join(dest_dir, name)
        with self.lock:
            target = sqlite3.connect(dest)
            try:
                self.conn.backup(target)
            finally:
                target.close()
        self.prune_backups(keep)
        return dest

    def prune_backups(self, dest_dir: str = "", keep: int = 7) -> int:
        try:
            keep = max(1, int(keep))
        except (TypeError, ValueError):
            keep = 7
        dest_dir = str(dest_dir or self.backup_dir())
        try:
            names = [n for n in os.listdir(dest_dir)
                     if n.startswith("amo_data-") and n.endswith(".db")]
        except Exception:
            return 0
        files = []
        for name in names:
            path = os.path.join(dest_dir, name)
            try:
                files.append((os.path.getmtime(path), path))
            except Exception:
                continue
        files.sort(reverse=True)
        removed = 0
        for _, path in files[keep:]:
            try:
                os.remove(path)
                removed += 1
            except Exception:
                continue
        return removed

    def list_backups(self, dest_dir: str = "") -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        dest_dir = str(dest_dir or self.backup_dir())
        try:
            names = os.listdir(dest_dir)
        except Exception:
            return out
        for name in names:
            if not (name.startswith("amo_data-") and name.endswith(".db")):
                continue
            path = os.path.join(dest_dir, name)
            try:
                st = os.stat(path)
            except Exception:
                continue
            out.append({"name": name, "size": int(st.st_size), "mtime": int(st.st_mtime)})
        out.sort(key=lambda item: item["name"], reverse=True)
        return out

    def restore_backup(self, name: str, bot_id: str = "") -> Dict[str, Any]:
        """从 backups/ 里的某个 .db 恢复（合并语义，不覆盖现有数据）。"""
        safe = os.path.basename(str(name or ""))
        path = os.path.join(self.backup_dir(), safe)
        try:
            snapshot = self.load_snapshot_file(path, bot_id)
            if not snapshot:
                return {"error": 1}
            return self.import_snapshot(snapshot)
        except Exception as exc:
            logger.debug("[amo_data] 恢复备份失败: %s" % exc)
            return {"error": 1}


def _now_str() -> str:
    import time
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())
