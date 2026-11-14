# -*- coding: utf-8 -*-
"""services/person_engine.py —— 人物、跨平台身份、关系网。"""

from __future__ import annotations

import sqlite3

from typing import Any, Dict, List

try:  # AstrBot 包模式
    from ..utils.text_process import (
        _bot_filter,
        _clean,
        _name_key,
        _now,
        _unique_labels,
        logger,
        platform_label,
    )
except ImportError:  # 顶层导入（测试/脚本）
    from utils.text_process import (
        _bot_filter,
        _clean,
        _name_key,
        _now,
        _unique_labels,
        logger,
        platform_label,
    )


class PersonEngine:
    """认人/绑定/合并/关系网；画像与记忆各归各的引擎。"""

    def __init__(self, db, settings=None, persons=None):
        self.db = db
        self.settings = settings if settings is not None else {}
        self.persons = persons
        self.plugin = None
        self._canonical_cache = {}

    # ---- 迁移期适配：老 mixin 里直接用了 self.conn / self.lock / self.setting ----
    @property
    def conn(self) -> sqlite3.Connection:
        return self.db.conn

    @property
    def lock(self):
        return self.db.lock

    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    # 中文时间词（原 DataStore 上的类属性，聊天时间窗解析要用）
    _CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
               "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    _PERIODS = (
        (("凌晨",), 0, 6),
        (("早上", "早晨", "上午"), 6, 12),
        (("中午",), 11, 14),
        (("下午",), 12, 18),
        (("傍晚", "晚上", "今晚", "昨晚", "夜里", "深夜"), 18, 24),
    )

    def resolve_person(
        self, bot_id: str, platform: str, uid: str, name: str = "", auto_merge: bool = True
    ) -> int:
        """拿到 (平台, uid) 对应的人物 pid：没有就建，同名就自动并过去。

        自动合并只在「同一个 bot、名字归一化后完全一样、并且只命中一个人」时发生，
        宁可少并错，不可乱并；并错了面板可以解绑。
        """
        bot_id, platform, uid = str(bot_id), str(platform or ""), str(uid)
        if not uid:
            return 0
        platform, uid = self._alias_of(bot_id, platform, uid)
        display = _clean(name, 64)
        with self.lock:
            row = self.conn.execute(
                "SELECT pid FROM person_links WHERE bot_id=? AND platform=? AND uid=?",
                (bot_id, platform, uid),
            ).fetchone()
            if row:
                pid = int(row["pid"] or 0)
                if pid:
                    if display:
                        self.conn.execute(
                            "UPDATE person_links SET name=? WHERE bot_id=? AND platform=? AND uid=?",
                            (display, bot_id, platform, uid),
                        )
                        self.conn.execute(
                            "UPDATE persons SET name=?, updated_at=? WHERE pid=?",
                            (display, _now(), pid),
                        )
                        self.conn.commit()
                    return pid
            target = 0
            # 同一个 uid 出现在别的平台（导入的飞书记录里 subject_id 还是 QQ 号）：
            # 并到已有那个人，不另起一个「他」。
            same = self.conn.execute(
                "SELECT pid FROM person_links WHERE bot_id=? AND uid=? AND pid>0 LIMIT 1",
                (bot_id, uid),
            ).fetchone()
            if same and int(same["pid"] or 0):
                target = int(same["pid"])
            key = _name_key(display)
            if not target and auto_merge and len(key) >= 2:
                rows = self.conn.execute(
                    "SELECT pid, name FROM person_links WHERE bot_id=? AND pid>0 AND platform!=?",
                    (bot_id, platform),
                ).fetchall()
                pids = {
                    int(item["pid"]) for item in rows
                    if int(item["pid"] or 0) > 0 and _name_key(item["name"]) == key
                }
                if len(pids) == 1:
                    target = pids.pop()
            if not target:
                cur = self.conn.execute(
                    "INSERT INTO persons(bot_id, name, created_at, updated_at) VALUES(?,?,?,?)",
                    (bot_id, display, _now(), _now()),
                )
                target = int(cur.lastrowid or 0)
            self.conn.execute(
                "INSERT INTO person_links(bot_id, platform, uid, pid, name, created_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id, platform, uid) DO UPDATE SET "
                "pid=excluded.pid, name=CASE WHEN excluded.name!='' THEN excluded.name ELSE person_links.name END",
                (bot_id, platform, uid, target, display, _now()),
            )
            self._backfill_messages(bot_id, platform, uid, target)
            self.conn.commit()
            return target


    def _backfill_messages(self, bot_id: str, platform: str, uid: str, pid: int) -> None:
        try:
            self.conn.execute(
                "UPDATE messages SET pid=?, platform=CASE WHEN platform='' THEN ? ELSE platform END "
                "WHERE bot_id=? AND uid=? AND (pid=0 OR pid IS NULL) "
                "AND (platform=? OR platform='')",
                (int(pid), str(platform), str(bot_id), str(uid), str(platform)),
            )
        except Exception:
            pass


    def link_identity(self, bot_id: str, platform: str, uid: str, pid: int = 0,
                      name: str = "") -> int:
        """手动把某个 (平台, uid) 绑到指定人物；pid=0 表示新建人物。"""
        bot_id, platform, uid = str(bot_id), str(platform or ""), str(uid)
        if not uid:
            return 0
        self.clear_canonical_cache()
        with self.lock:
            pid = int(pid or 0)
            if pid <= 0:
                cur = self.conn.execute(
                    "INSERT INTO persons(bot_id, name, created_at, updated_at) VALUES(?,?,?,?)",
                    (bot_id, _clean(name, 64), _now(), _now()),
                )
                pid = int(cur.lastrowid or 0)
            row = self.conn.execute(
                "SELECT pid FROM person_links WHERE bot_id=? AND platform=? AND uid=?",
                (bot_id, platform, uid),
            ).fetchone()
            if row and int(row["pid"] or 0) and int(row["pid"]) != pid:
                left = self.conn.execute(
                    "SELECT COUNT(*) AS c FROM person_links WHERE bot_id=? AND pid=?",
                    (bot_id, int(row["pid"])),
                ).fetchone()
                old_pid = int(row["pid"])
                if left is not None and int(left["c"] or 0) <= 1:
                    self.conn.execute("DELETE FROM persons WHERE pid=?", (old_pid,))
            self.conn.execute(
                "INSERT INTO person_links(bot_id, platform, uid, pid, name, created_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id, platform, uid) DO UPDATE SET "
                "pid=excluded.pid, name=CASE WHEN excluded.name!='' THEN excluded.name ELSE person_links.name END",
                (bot_id, platform, uid, pid, _clean(name, 64), _now()),
            )
            self._backfill_messages(bot_id, platform, uid, pid)
            self.conn.commit()
            return pid


    def unlink_identity(self, bot_id: str, platform: str, uid: str) -> bool:
        """解绑：这个身份单独变成一个人物，不删聊天记录。"""
        self.clear_canonical_cache()
        with self.lock:
            row = self.conn.execute(
                "SELECT pid FROM person_links WHERE bot_id=? AND platform=? AND uid=?",
                (str(bot_id), str(platform or ""), str(uid)),
            ).fetchone()
            if not row:
                return False
            old_pid = int(row["pid"] or 0)
            cur = self.conn.execute(
                "INSERT INTO persons(bot_id, name, created_at, updated_at) VALUES(?,?,?,?)",
                (str(bot_id), "", _now(), _now()),
            )
            pid = int(cur.lastrowid or 0)
            self.conn.execute(
                "UPDATE person_links SET pid=? WHERE bot_id=? AND platform=? AND uid=?",
                (pid, str(bot_id), str(platform or ""), str(uid)),
            )
            self.conn.execute(
                "UPDATE messages SET pid=? WHERE bot_id=? AND pid=? AND platform=? AND uid=?",
                (pid, str(bot_id), old_pid, str(platform or ""), str(uid)),
            )
            if old_pid:
                left = self.conn.execute(
                    "SELECT COUNT(*) AS c FROM person_links WHERE bot_id=? AND pid=?",
                    (str(bot_id), old_pid),
                ).fetchone()
                if left is not None and int(left["c"] or 0) == 0:
                    self.conn.execute("DELETE FROM persons WHERE pid=?", (old_pid,))
            self.conn.commit()
            return True


    def merge_persons(self, bot_id: str, keep_pid: int, drop_pid: int) -> bool:
        """把 drop 合并进 keep：身份、聊天记录、记忆都并过去，然后删掉 drop。"""
        self.clear_canonical_cache()
        bot_id = str(bot_id)
        keep_pid, drop_pid = int(keep_pid or 0), int(drop_pid or 0)
        if not keep_pid or not drop_pid or keep_pid == drop_pid:
            return False
        with self.lock:
            exists = self.conn.execute(
                "SELECT COUNT(*) AS c FROM persons WHERE bot_id=? AND pid IN (?,?)",
                (bot_id, keep_pid, drop_pid),
            ).fetchone()
            if not exists or int(exists["c"] or 0) < 2:
                return False
            for table, column in (("person_links", "pid"), ("messages", "pid")):
                try:
                    self.conn.execute(
                        "UPDATE %s SET %s=? WHERE bot_id=? AND %s=?" % (table, column, column),
                        (keep_pid, bot_id, drop_pid),
                    )
                except Exception:
                    continue
            # 记忆是按 uid 存的，合并时并到保留人物的第一个 uid 上
            keep_links = self.person_links(bot_id, keep_pid)
            drop_links = self.person_links(bot_id, drop_pid)
            keep_uid = keep_links[0]["uid"] if keep_links else ""
            if keep_uid:
                for item in drop_links:
                    rows = self.conn.execute(
                        "SELECT id, idem_key FROM memories WHERE bot_id=? AND uid=?",
                        (bot_id, item["uid"]),
                    ).fetchall()
                    for mem in rows:
                        try:
                            self.conn.execute(
                                "UPDATE memories SET uid=? WHERE bot_id=? AND id=?",
                                (keep_uid, bot_id, int(mem["id"])),
                            )
                        except sqlite3.IntegrityError:
                            # 两边有同一条记忆：把旧的标 merged 挂到保留行，别让它变成孤儿
                            conflict = self.conn.execute(
                                "SELECT id FROM memories WHERE bot_id=? AND uid=? AND idem_key=?",
                                (bot_id, keep_uid, mem["idem_key"]),
                            ).fetchone()
                            self.conn.execute(
                                "UPDATE memories SET status='merged', superseded_by=? "
                                "WHERE bot_id=? AND id=?",
                                (int(conflict["id"]) if conflict else None, bot_id, int(mem["id"])),
                            )
            self.conn.execute("DELETE FROM persons WHERE bot_id=? AND pid=?", (bot_id, drop_pid))
            self.conn.execute(
                "UPDATE persons SET updated_at=? WHERE pid=?", (_now(), keep_pid)
            )
            self.conn.commit()
            return True


    def person_links(self, bot_id: str, pid: int) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT platform, uid, name, created_at FROM person_links "
                "WHERE bot_id=? AND pid=? ORDER BY created_at",
                (str(bot_id), int(pid)),
            ).fetchall()
        return [
            {
                "platform": str(row["platform"] or ""),
                "label": platform_label(str(row["platform"] or "")),
                "uid": str(row["uid"] or ""),
                "name": str(row["name"] or ""),
                "created_at": int(row["created_at"] or 0),
            }
            for row in rows
        ]


    def list_persons(self, bot_id: str = "", keyword: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        where, params = _bot_filter(bot_id)
        sql = "SELECT pid, bot_id, name, updated_at FROM persons WHERE 1=1" + where
        if keyword:
            like = "%" + str(keyword) + "%"
            sql += (" AND (name LIKE ? OR pid IN (SELECT pid FROM person_links "
                    "WHERE bot_id=persons.bot_id AND (uid LIKE ? OR name LIKE ?)))")
            params.extend([like, like, like])
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
            out: List[Dict[str, Any]] = []
            for row in rows:
                pid = int(row["pid"] or 0)
                bot = str(row["bot_id"] or "")
                links = self.person_links(bot, pid)
                stats = self.conn.execute(
                    "SELECT COUNT(*) AS c, MAX(created_at) AS last FROM messages "
                    "WHERE bot_id=? AND pid=?",
                    (bot, pid),
                ).fetchone()
                out.append({
                    "pid": pid,
                    "bot_id": bot,
                    "name": str(row["name"] or "") or next(
                        (item["name"] for item in links if item["name"]), ""
                    ),
                    "links": links,
                    "platforms": _unique_labels(links),
                    "msg_count": int(stats["c"] or 0) if stats else 0,
                    "last_at": int(stats["last"] or 0) if stats else 0,
                    "updated_at": int(row["updated_at"] or 0),
                })
        return out


    def person_detail(self, bot_id: str, pid: int) -> Dict[str, Any]:
        with self.lock:
            row = self.conn.execute(
                "SELECT pid, bot_id, name, created_at, updated_at FROM persons WHERE pid=?",
                (int(pid),),
            ).fetchone()
            if not row:
                return {}
            bot = str(row["bot_id"] or "")
            links = self.person_links(bot, int(pid))
            stats = self.conn.execute(
                "SELECT COUNT(*) AS c, MAX(created_at) AS last FROM messages WHERE bot_id=? AND pid=?",
                (bot, int(pid)),
            ).fetchone()
            profiles = []
            for item in links:
                profile = self.plugin.profiles.get_profile(bot, item["uid"])
                if profile.get("exists"):
                    profiles.append(profile)
            rows = self.conn.execute(
                "SELECT platform, uid, name, role, content, created_at, compressed, group_name "
                "FROM messages WHERE bot_id=? AND pid=? ORDER BY created_at DESC LIMIT 60",
                (bot, int(pid)),
            ).fetchall()
            recent = []
            for item in rows:
                info = dict(item)
                info["label"] = platform_label(str(info.get("platform") or ""))
                recent.append(info)
        return {
            "pid": int(row["pid"]),
            "bot_id": bot,
            "name": str(row["name"] or ""),
            "links": links,
            "platforms": [item["label"] for item in links],
            "profiles": profiles,
            "recent": recent,
            "msg_count": int(stats["c"] or 0) if stats else 0,
            "last_at": int(stats["last"] or 0) if stats else 0,
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
        }


    def _alias_of(self, bot_id: str, platform: str, uid: str):
        """把一个 (平台, uid) 换成规范身份；没配别名就原样返回。"""
        platform, uid = str(platform or ""), str(uid)
        try:
            with self.lock:
                row = self.conn.execute(
                    "SELECT alias_platform, alias_uid FROM identity_alias "
                    "WHERE bot_id=? AND platform=? AND uid=?",
                    (str(bot_id), platform, uid),
                ).fetchone()
        except Exception:
            return (platform, uid)
        if row and str(row["alias_platform"] or "").strip():
            return (str(row["alias_platform"]), str(row["alias_uid"]))
        return (platform, uid)


    def set_identity_alias(self, bot_id: str, platform: str, uid: str,
                           alias_platform: str = "", alias_uid: str = "") -> bool:
        """登记「导出的旧身份」→「规范身份」，之后 resolve_person 会自动走别名。"""
        try:
            with self.lock:
                self.conn.execute(
                    "INSERT INTO identity_alias(bot_id, platform, uid, alias_platform, alias_uid, created_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id, platform, uid) DO UPDATE SET "
                    "alias_platform=excluded.alias_platform, alias_uid=excluded.alias_uid",
                    (str(bot_id), str(platform), str(uid), str(alias_platform),
                     str(alias_uid), _now()),
                )
                self.conn.commit()
            # 别名变了，缓存的"主 uid"立刻作废，否则运行期新加的绑定要等重启才生效
            self.clear_canonical_cache()
            return True
        except Exception:
            return False


    def list_identity_aliases(self, bot_id: str = "") -> List[Dict[str, Any]]:
        where, params = _bot_filter(bot_id)
        try:
            with self.lock:
                rows = self.conn.execute(
                    "SELECT bot_id, platform, uid, alias_platform, alias_uid FROM identity_alias "
                    "WHERE 1=1" + where + " ORDER BY platform, uid", params,
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception:
            return []


    def merge_ghost_persons(self) -> int:
        """把「只有摘要、没有档案也没有记忆」的幽灵人物并进唯一有档案的那个人。

        来历：历史迁移把摘要挂到了一个没有档案的 pid 上（换绑时人物被删、链接留下），
        面板里就多出一个只有 10 条摘要的空人。
        条件收得很窄：全库只有一个带 profile 的人；候选没有 profile、没有记忆、
        也不含任何非摘要消息。并错了可以在面板对那条身份「解绑」还原。
        """
        merged = 0
        with self.lock:
            try:
                profiled = set()
                for row in self.conn.execute(
                    "SELECT DISTINCT bot_id, uid FROM profiles"
                ).fetchall():
                    profiled.add((str(row["bot_id"]), str(row["uid"])))
                if not profiled:
                    return 0
                links = self.conn.execute(
                    "SELECT bot_id, platform, uid, pid FROM person_links WHERE pid>0"
                ).fetchall()
                keep_by_bot = {}
                for row in links:
                    if (str(row["bot_id"]), str(row["uid"])) in profiled:
                        keep_by_bot[str(row["bot_id"])] = int(row["pid"])
                if not keep_by_bot:
                    return 0
                persons = self.conn.execute(
                    "SELECT pid, bot_id, name FROM persons ORDER BY pid"
                ).fetchall()
            except Exception:
                return 0
            for bot_id, keep_pid in keep_by_bot.items():
                for person in persons:
                    pid = int(person["pid"])
                    if str(person["bot_id"]) != bot_id or pid == keep_pid:
                        continue
                    has_mem = self.conn.execute(
                        "SELECT COUNT(*) AS n FROM memories WHERE bot_id=? AND pid=?",
                        (bot_id, pid),
                    ).fetchone()
                    if has_mem and int(has_mem["n"] or 0) > 0:
                        continue
                    other = self.conn.execute(
                        "SELECT COUNT(*) AS n FROM messages WHERE bot_id=? AND pid=? "
                        "AND COALESCE(role,'user') NOT IN ('summary', 'system')",
                        (bot_id, pid),
                    ).fetchone()
                    if other and int(other["n"] or 0) > 0:
                        continue
                    if self.merge_persons(bot_id, keep_pid, pid):
                        merged += 1
                        logger.warning(
                            "[amo_data] 幽灵人物已并入 pid=%s：pid=%s name=%s",
                            keep_pid, pid, str(person["name"] or ""),
                        )
        return merged


    def repair_persons(self) -> int:
        """人物表自愈：身份指向一个已经不存在的人物时，把人物补回来。

        以前换绑/清理逻辑会删掉 persons 行却把 person_links 留着，
        表现就是「人物点开是空的」「消息挂在幽灵 pid 上」。
        """
        fixed = 0
        with self.lock:
            try:
                rows = self.conn.execute(
                    "SELECT l.pid, l.bot_id, l.name, l.created_at FROM person_links l "
                    "LEFT JOIN persons p ON p.pid = l.pid WHERE p.pid IS NULL AND l.pid > 0"
                ).fetchall()
            except Exception:
                return 0
            for row in rows:
                try:
                    self.conn.execute(
                        "INSERT INTO persons(pid, bot_id, name, created_at, updated_at) "
                        "VALUES(?,?,?,?,?)",
                        (int(row["pid"]), str(row["bot_id"]), str(row["name"] or ""),
                         int(row["created_at"] or _now()), _now()),
                    )
                    fixed += 1
                except Exception:
                    continue
            if fixed:
                self.conn.commit()
                logger.warning("[amo_data] 人物表自愈：补回 %d 个丢失的人物" % fixed)
        self.clear_canonical_cache()
        return fixed


    def adopt_orphan_memories(self) -> int:
        """没有身份记录的记忆（pid=0 且 uid 不在 person_links 里）。

        库里只有一个人时不猜也错不了：整库归给他，uid 归一成主 uid。
        多个人时不动（宁可不并，也不错并），只打一条日志等人来绑。
        """
        adopted = 0
        with self.lock:
            try:
                uids = [str(r["uid"]) for r in self.conn.execute(
                    "SELECT DISTINCT uid FROM memories WHERE COALESCE(pid,0)=0 "
                    "AND uid NOT IN ('', 'bot')"
                ).fetchall()]
            except Exception:
                return 0
            if not uids:
                return 0
            linked = {(str(r["bot_id"]), str(r["uid"])) for r in self.conn.execute(
                "SELECT bot_id, uid FROM person_links"
            ).fetchall()}
            # 机器人自己的 uid（identity_alias 里 self 指向的那个）
            self_uids = set()
            try:
                for row in self.conn.execute(
                    "SELECT alias_uid FROM identity_alias WHERE uid='self' AND alias_uid!=''"
                ).fetchall():
                    self_uids.add(str(row["alias_uid"]))
            except Exception:
                self_uids = set()
            persons = self.conn.execute("SELECT pid, bot_id FROM persons ORDER BY pid").fetchall()
            for uid in uids:
                if any(u == uid for (_b, u) in linked):
                    continue
                rows = self.conn.execute(
                    "SELECT id, bot_id, scope, group_id, idem_key FROM memories "
                    "WHERE COALESCE(pid,0)=0 AND uid=?", (uid,)
                ).fetchall()
                for mem in rows:
                    bot_id = str(mem["bot_id"])
                    same_bot = [p for p in persons if str(p["bot_id"]) == bot_id]
                    pid = 0
                    if uid in self_uids:
                        # 这个 uid 是机器人自己：当年按 self 记账的记忆其实是「关于主人」的，
                        # 归给消息最多的那个人（主人），不按"只有一个人物"去猜
                        owner = self.conn.execute(
                            "SELECT pid, COUNT(*) AS n FROM messages WHERE bot_id=? AND pid>0 "
                            "GROUP BY pid ORDER BY n DESC LIMIT 1",
                            (bot_id,),
                        ).fetchone()
                        pid = int(owner["pid"] or 0) if owner else 0
                    elif len(same_bot) == 1:
                        pid = int(same_bot[0]["pid"])
                    if not pid:
                        logger.warning(
                            "[amo_data] 孤儿记忆未处理：uid=%s 有 %d 个人物，等手工绑定",
                            uid, len(same_bot),
                        )
                        continue
                    links = [dict(i) for i in self.conn.execute(
                        "SELECT platform, uid FROM person_links WHERE bot_id=? AND pid=?",
                        (bot_id, pid)).fetchall()]
                    canonical = self._canonical_link(links, uid) or uid
                    try:
                        self.conn.execute(
                            "UPDATE memories SET uid=?, pid=? WHERE id=?",
                            (canonical, pid, int(mem["id"])),
                        )
                        adopted += 1
                    except sqlite3.IntegrityError:
                        old = self.conn.execute(
                            "SELECT id FROM memories WHERE bot_id=? AND uid=? AND scope=? "
                            "AND group_id=? AND idem_key=? LIMIT 1",
                            (bot_id, canonical, str(mem["scope"]), str(mem["group_id"]),
                             str(mem["idem_key"])),
                        ).fetchone()
                        self.conn.execute(
                            "UPDATE memories SET status='merged', superseded_by=? WHERE id=?",
                            (int(old["id"]) if old else 0, int(mem["id"])),
                        )
            if adopted:
                self.conn.commit()
                logger.info("[amo_data] 孤儿记忆归位 %d 条" % adopted)
        return adopted


    def pid_for_uid(self, bot_id: str, uid: str) -> int:
        """只有 uid 时反查 pid（不分平台）；查不到返回 0。"""
        uid = str(uid or "")
        if not uid:
            return 0
        try:
            with self.lock:
                row = self.conn.execute(
                    "SELECT pid FROM person_links WHERE bot_id=? AND uid=? AND pid>0 LIMIT 1",
                    (str(bot_id), uid),
                ).fetchone()
        except Exception:
            return 0
        return int(row["pid"] or 0) if row else 0


    def person_pid(self, bot_id: str, platform: str, uid: str) -> int:
        """(平台, uid) → pid；先过别名表。只查不建，查不到返回 0。"""
        platform, uid = self._alias_of(str(bot_id), str(platform or ""), str(uid))
        if not uid:
            return 0
        try:
            with self.lock:
                row = self.conn.execute(
                    "SELECT pid FROM person_links WHERE bot_id=? AND platform=? AND uid=?",
                    (str(bot_id), platform, uid),
                ).fetchone()
        except Exception:
            return 0
        return int(row["pid"] or 0) if row else 0


    def _canonical_link(self, links, fallback: str = "") -> str:
        """同一个人选一个主身份：优先飞书 open_id，其次第一条。"""
        for item in links or []:
            platform = str(item.get("platform") or "")
            uid = str(item.get("uid") or "")
            if platform == "lark" and uid:
                return uid
        for item in links or []:
            uid = str(item.get("uid") or "")
            if uid:
                return uid
        return str(fallback or "")


    def _canonical_cache_get(self):
        cache = getattr(self, "_canonical_cache", None)
        if cache is None:
            cache = {}
            self._canonical_cache = cache
        return cache


    def clear_canonical_cache(self) -> None:
        self._canonical_cache = {}


    def canonical_uid(self, bot_id: str, platform: str, uid: str) -> str:
        """把任意平台身份换成这个人的主 uid（飞书优先），写消息/记忆时用。"""
        bot_id = str(bot_id)
        platform, uid = self._alias_of(bot_id, str(platform or ""), str(uid))
        if not uid:
            return ""
        cache = self._canonical_cache_get()
        key = (bot_id, platform, uid)
        if key in cache:
            return cache[key]
        with self.lock:
            row = self.conn.execute(
                "SELECT pid FROM person_links WHERE bot_id=? AND platform=? AND uid=?",
                (str(bot_id), platform, uid),
            ).fetchone()
            if not row or not int(row["pid"] or 0):
                return uid
            links = self.conn.execute(
                "SELECT platform, uid FROM person_links WHERE bot_id=? AND pid=?",
                (str(bot_id), int(row["pid"])),
            ).fetchall()
        result = self._canonical_link([dict(item) for item in links], uid)
        cache[key] = result
        return result


    def canonical_uid_for_uid(self, bot_id: str, uid: str) -> str:
        """没有平台信息时（remember 只带 uid）按 uid 找主身份。"""
        uid = str(uid or "")
        if not uid:
            return ""
        bot_id = str(bot_id)
        cache = self._canonical_cache_get()
        key = (bot_id, "__uid__", uid)
        if key in cache:
            return cache[key]
        with self.lock:
            row = self.conn.execute(
                "SELECT pid FROM person_links WHERE bot_id=? AND uid=? AND pid>0 LIMIT 1",
                (bot_id, uid),
            ).fetchone()
            if not row:
                return uid
            links = self.conn.execute(
                "SELECT platform, uid FROM person_links WHERE bot_id=? AND pid=?",
                (str(bot_id), int(row["pid"])),
            ).fetchall()
        result = self._canonical_link([dict(item) for item in links], uid)
        cache[key] = result
        return result


    def normalize_identities(self, bot_id: str = "") -> int:
        """把旧数据里飞书 open_id / QQ 号混用的 uid 统一成主 uid。"""
        if not str(bot_id or ""):
            # 启动时不知道 bot_id：按 persons 里出现过的 bot 逐个归一
            try:
                with self.lock:
                    bots = [str(r["bot_id"]) for r in self.conn.execute(
                        "SELECT DISTINCT bot_id FROM persons WHERE bot_id!=''"
                    ).fetchall()]
            except Exception:
                bots = []
            return sum(self.normalize_identities(bid) for bid in bots if bid)
        where, params = _bot_filter(bot_id)
        with self.lock:
            pids = [int(r["pid"]) for r in self.conn.execute(
                "SELECT DISTINCT pid FROM person_links WHERE pid>0" + where, params
            ).fetchall()]
            changed = 0
            for pid in pids:
                links = self.conn.execute(
                    "SELECT platform, uid FROM person_links WHERE pid=?", (pid,)
                ).fetchall()
                links = [dict(item) for item in links]
                if not links:
                    continue
                canonical = self._canonical_link(links)
                if not canonical:
                    continue
                for item in links:
                    uid = str(item.get("uid") or "")
                    if not uid:
                        continue
                    try:
                        cur = self.conn.execute(
                            "UPDATE messages SET uid=?, pid=? WHERE bot_id=? AND uid=? "
                            "AND (pid=0 OR pid IS NULL)",
                            (canonical, pid, str(bot_id), uid),
                        )
                        changed += max(0, cur.rowcount or 0)
                        if uid != canonical:
                            cur = self.conn.execute(
                                "UPDATE messages SET uid=? WHERE bot_id=? AND pid=? AND role!='bot' AND uid=?",
                                (canonical, str(bot_id), pid, uid),
                            )
                            changed += max(0, cur.rowcount or 0)
                            cur = self.conn.execute(
                                "UPDATE memories SET uid=? WHERE bot_id=? AND uid=?",
                                (canonical, str(bot_id), uid),
                            )
                            changed += max(0, cur.rowcount or 0)
                    except sqlite3.IntegrityError as exc:
                        # 撞唯一索引（idx_mem_idem 含 uid）时只跳过这一条，
                        # 以前会让整个归一事务半途而废、后面的身份永远留在旧 uid
                        logger.warning("[amo_data] 身份归一冲突 uid=%s pid=%s: %s", uid, pid, exc)
                        continue
            self.conn.commit()
            self.clear_canonical_cache()
            return changed


    def person_of(self, bot_id: str, platform: str, uid: str) -> Dict[str, Any]:
        platform, uid = self._alias_of(str(bot_id), str(platform or ""), str(uid))
        with self.lock:
            row = self.conn.execute(
                "SELECT pid FROM person_links WHERE bot_id=? AND platform=? AND uid=?",
                (str(bot_id), str(platform or ""), str(uid)),
            ).fetchone()
        if not row:
            return {}
        return self.person_detail(str(bot_id), int(row["pid"] or 0))




    def bootstrap(self) -> None:
        """启动自愈（原 store.__init__ 里做的那几件事）。失败只告警，不挡启动。"""
        for fn in (self.normalize_identities, self.repair_persons,
                   self.adopt_orphan_memories, self.merge_ghost_persons):
            try:
                fn()
            except Exception as exc:
                # 以前这里 except: pass，身份归一/记忆归位失败完全看不出来
                logger.warning("[amo_data] 启动自愈 %s 失败: %s",
                               getattr(fn, "__name__", fn), exc)

    def person_context(
        self, bot_id: str, platform: str, uid: str, limit: int = 8
    ) -> Dict[str, Any]:
        """给 Amo 用：这个人在别的平台说过什么，一起带回来。"""
        detail = self.person_of(bot_id, platform, uid)
        if not detail:
            return {}
        pairs = [(item["platform"], item["uid"]) for item in detail.get("links", [])]
        others = [item for item in pairs if not (item[0] == str(platform) and item[1] == str(uid))]
        recent: List[Dict[str, Any]] = []
        # 每个平台各取 recent_limit 条，再合并：只按全局时间取的话，
        # 聊得多的那个平台会把另一个平台挤没（跨平台上下文就空了）。
        per_platform = max(1, min(int(limit), 50))
        with self.lock:
            gathered: Dict[tuple, sqlite3.Row] = {}
            # 他的话：按 (平台, uid) 取，每个平台各 per_platform 条；
            # 她的话：role='bot' + 同一个 pid（跨平台都算），单独限量。
            # 混在一条 SQL 里时她的话会把他的近聊挤出 limit（跨平台块就只剩她的发言）。
            seen_platforms = set()
            for item in detail.get("links", []) or []:
                # 按 pid + 平台取，不按 uid：飞书消息可能用归一后的 QQ uid 落库，
                # 只按 open_id 查会漏掉最新几条，跨平台上下文就只剩旧消息。
                platform = str(item.get("platform") or "")
                if not platform or platform in seen_platforms:
                    continue
                seen_platforms.add(platform)
                rows = self.conn.execute(
                    "SELECT platform, uid, name, role, content, created_at, compressed, group_name "
                    "FROM messages WHERE bot_id=? AND pid=? AND platform=? AND role!='bot' "
                    "ORDER BY created_at DESC LIMIT ?",
                    (str(bot_id), int(detail["pid"]), platform, per_platform),
                ).fetchall()
                for row in rows:
                    gathered[(row["platform"], row["role"], row["created_at"], row["content"])] = row
            bot_rows = self.conn.execute(
                "SELECT platform, uid, name, role, content, created_at, compressed, group_name "
                "FROM messages WHERE bot_id=? AND pid=? AND role='bot' "
                "ORDER BY created_at DESC LIMIT ?",
                (str(bot_id), int(detail["pid"]), per_platform),
            ).fetchall()
            for row in bot_rows:
                gathered[(row["platform"], row["role"], row["created_at"], row["content"])] = row
            rows = sorted(gathered.values(), key=lambda row: int(row["created_at"] or 0), reverse=True)[: per_platform * 4]
            recent = [
                {
                    "platform": str(row["platform"] or ""),
                    "label": platform_label(str(row["platform"] or "")),
                    "uid": str(row["uid"] or ""),
                    "name": str(row["name"] or ""),
                    "role": str(row["role"] or "user"),
                    "content": str(row["content"] or ""),
                    "group_name": str(row["group_name"] or ""),
                    "created_at": int(row["created_at"] or 0),
                    "compressed": int(row["compressed"] or 0),
                }
                for row in rows
            ]
        memories: List[Dict[str, Any]] = []
        try:
            # 优先按 pid 取（同一人的所有平台记忆都在这个 pid 下）；
            # 老记忆 pid=0，再按各个身份的 uid 兜一遍。
            pid_value = int(detail.get("pid") or 0)
            seen_mem = set()

            def _take(rows):
                for row in rows or []:
                    key = (str(row.get("content") or "")[:80], row.get("created_at"))
                    if key in seen_mem:
                        continue
                    seen_mem.add(key)
                    memories.append(row)

            if pid_value:
                _take(self.plugin.memory.list_memories(str(bot_id), limit=limit, pid=pid_value))
            uid_candidates = [str(uid)] + [str(item.get("uid") or "") for item in detail.get("links", [])]
            seen_uids = set()
            for one_uid in uid_candidates:
                if not one_uid or one_uid in seen_uids:
                    continue
                seen_uids.add(one_uid)
                _take(self.plugin.memory.list_memories(str(bot_id), uid=one_uid, limit=limit))
            memories = memories[: limit]
        except Exception:
            memories = []
        detail["others"] = [
            {"platform": item[0], "label": platform_label(item[0]), "uid": item[1]}
            for item in others
        ]
        detail["recent"] = recent
        detail["memories"] = memories
        return detail


    def _display_name(self, bot_id: str, uid: Any) -> str:
        """关系图上的显示名：先画像，再人物卡，最后空。"""
        uid = str(uid or "")
        if not uid:
            return ""
        try:
            with self.lock:
                row = self.conn.execute(
                    "SELECT name FROM profiles WHERE bot_id=? AND uid=? AND name!=''", (bot_id, uid)
                ).fetchone()
            if row and str(row["name"] or "").strip():
                return str(row["name"]).strip()
        except Exception:
            pass
        try:
            with self.lock:
                row = self.conn.execute(
                    "SELECT p.name FROM person_links l JOIN persons p ON p.pid=l.pid "
                    "WHERE l.bot_id=? AND l.uid=? AND p.name!='' LIMIT 1", (bot_id, uid)
                ).fetchone()
            if row and str(row["name"] or "").strip():
                return str(row["name"]).strip()
        except Exception:
            pass
        return ""


    def list_relations(self, bot_id: str, uid: str = "") -> List[Dict[str, Any]]:
        where, params = _bot_filter(bot_id)
        sql = "SELECT a_uid, b_uid, relation, note, updated_at FROM relations WHERE 1=1" + where
        if uid:
            sql += " AND (a_uid=? OR b_uid=?)"
            params.extend([str(uid), str(uid)])
        sql += " ORDER BY updated_at DESC"
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["a_name"] = self._display_name(str(bot_id), item.get("a_uid"))
            item["b_name"] = self._display_name(str(bot_id), item.get("b_uid"))
            out.append(item)
        return out


    def save_relation(
        self, bot_id: str, a_uid: str, b_uid: str, relation: str, note: str = ""
    ) -> bool:
        a_uid, b_uid = str(a_uid), str(b_uid)
        if not a_uid or not b_uid or a_uid == b_uid:
            return False
        with self.lock:
            self.conn.execute(
                "INSERT INTO relations(bot_id, a_uid, b_uid, relation, note, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id, a_uid, b_uid) DO UPDATE SET "
                "relation=excluded.relation, note=excluded.note, updated_at=excluded.updated_at",
                (str(bot_id), a_uid, b_uid, _clean(relation, 32) or "朋友", _clean(note, 200), _now()),
            )
            self.conn.commit()
        return True


    def delete_relation(self, bot_id: str, a_uid: str, b_uid: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM relations WHERE bot_id=? AND a_uid=? AND b_uid=?",
                (str(bot_id), str(a_uid), str(b_uid)),
            )
            self.conn.commit()
            return bool(cur.rowcount)

