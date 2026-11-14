# -*- coding: utf-8 -*-
"""core/web_handlers.py —— 面板 handler：解析参数 -> 调 services。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Optional

from astrbot.api import logger

try:  # 包模式
    from .web_request import web_request
except ImportError:  # 顶层导入（测试）
    from core.web_request import web_request

try:  # 顶层导入（测试）
    from ..utils.config import PLUGIN_NAME, PLUGIN_VERSION
except ImportError:
    from utils.config import PLUGIN_NAME, PLUGIN_VERSION


class WebHandlersMixin:
    """34 条面板接口的实现（写操作只收 POST，由 WebAPIMixin 守卫）。"""

    def _amo_plugin(self):
        """拿陪伴插件实例（没装/没加载就返回 None），用于同步删除画像。"""
        try:
            getter = getattr(self.context, "get_registered_star", None)
            meta = None
            if callable(getter):
                for _name in ("Astrbot_amoupup_forever", "Astrbot_Amo_forever"):
                    try:
                        meta = getter(_name)
                    except Exception:
                        meta = None
                    if meta is not None:
                        break
            return getattr(meta, "star_cls", None) if meta is not None else None
        except Exception as exc:
            logger.debug("[amo_data] 查找陪伴插件失败: %s" % exc)
            return None


    def _forget_in_amo(self, uid: str) -> bool:
        """把画像也从陪伴插件本地删掉，否则它 10 分钟一次的画像对账会把它推回来。"""
        amo = self._amo_plugin()
        db = getattr(amo, "db", None) if amo is not None else None
        remover = getattr(db, "delete_profile", None)
        if not callable(remover):
            return False
        try:
            remover(str(uid))
            logger.info("[amo_data] 已同步删除陪伴侧本地画像: %s" % uid)
            return True
        except Exception as exc:
            logger.warning("[amo_data] 同步删除陪伴侧画像失败(%s): %s" % (uid, exc))
            return False


    async def _body(self) -> dict:
        try:
            if web_request is not None and hasattr(web_request, "json"):
                data = await web_request.json(default={})
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        try:
            if web_request is not None:
                data = await web_request.get_json(silent=True)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}



    async def api_memory_maintain(self):
        """面板/脚本手动触发整理；payload: {apply: false} 可先预演。"""
        payload = await self._body()
        bot_id = self._bot_id(payload)
        apply = payload.get("apply", True)
        if isinstance(apply, str):
            apply = apply.strip().lower() not in ("0", "false", "no", "off")
        out = {"compress": await self.mem.compress_memories(bot_id, apply=bool(apply)),
               "archive": await self.mem.archive_stale(bot_id, 90, 0.3, apply=bool(apply))}
        return {"code": 0, "data": out, "applied": bool(apply)}


    async def api_status(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        return {
            "code": 0,
            "data": {
                "version": PLUGIN_VERSION,
                "bot_id": bot_id,
                "db": self.db_path,
                "counts": self.maintenance.stats(bot_id),
                "bots": self.db.bot_ids(),
                "embedding": self.memory.embedding_stats(),
                "recall": {
                    "pool_size": self.settings.get("pool_size"),
                    "min_matched_terms": self.settings.get("min_matched_terms"),
                    "half_life_days": self.settings.get("forget_half_life_days"),
                    "auto_dedupe": self.settings.get("auto_dedupe"),
                },
            },
        }


    async def api_profiles(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        keyword = str(payload.get("keyword") or payload.get("q") or "").strip()
        return {"code": 0, "data": {"items": self.profiles.list_profiles(bot_id, keyword, 500)}}


    async def api_profile(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return {"code": 1, "msg": "缺少 uid"}
        if not bot_id:
            # 面板默认聚合所有 bot，先找到这条画像属于哪个 bot，否则会读出空对象
            found = self.profiles.find_profile(uid)
            bot_id = str(found.get("bot_id") or "")
        return {
            "code": 0,
            "data": {
                "bot_id": bot_id,
                "profile": self.profiles.get_profile(bot_id, uid),
                "memories": self.memory.list_memories(bot_id, uid, 200),
            },
        }


    async def api_profile_save(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return {"code": 1, "msg": "缺少 uid"}
        if not str(payload.get("bot_id") or "").strip():
            # 改的应该是原来那条，不要另建到别的 bot 下
            found = self.profiles.find_profile(uid)
            bot_id = str(found.get("bot_id") or bot_id)
        profile = self.profiles.save_profile(
            bot_id, uid,
            name=payload.get("name"),
            alias=payload.get("alias"),
            relation=payload.get("relation"),
            remark=payload.get("remark"),
        )
        platform = str(payload.get("platform") or "").strip()
        if platform:
            self.persons.resolve_person(bot_id, platform, uid, str(profile.get("name") or ""))
        return {"code": 0, "data": profile, "msg": "已保存"}


    async def api_profile_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return {"code": 1, "msg": "缺少 uid"}
        if not str(payload.get("bot_id") or "").strip():
            found = self.profiles.find_profile(uid)
            bot_id = str(found.get("bot_id") or bot_id)
        if str(payload.get("confirm") or "") not in ("1", "true", "yes"):
            return {"code": 1, "msg": "删除需要确认：confirm=1"}
        logger.info("[amo_data] 删除画像请求: uid=%s bot=%s" % (uid, bot_id or "-"))
        counts = self.profiles.delete_profile(bot_id, uid)
        # 只删数据侧没用：陪伴插件（amo）本地还留着画像，它每 10 分钟会把
        # 「本地有、数据侧缺」的画像重新推回来，面板上看起来就是「删不掉」。
        self._forget_in_amo(uid)
        return {"code": 0, "data": counts, "msg": "已删除"}


    async def api_persons(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        keyword = str(payload.get("keyword") or payload.get("q") or "").strip()
        return {"code": 0, "data": {"items": self.persons.list_persons(bot_id, keyword, 300)}}


    async def api_person(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        pid = int(payload.get("pid") or 0)
        platform = str(payload.get("platform") or "").strip()
        uid = str(payload.get("uid") or "").strip()
        if not pid and platform and uid:
            detail = self.persons.person_of(bot_id, platform, uid)
        else:
            detail = self.persons.person_detail(bot_id, pid) if pid else {}
        if not detail:
            return {"code": 1, "msg": "没找到这个人"}
        return {"code": 0, "data": detail}


    async def api_person_link(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        platform = str(payload.get("platform") or "").strip()
        uid = str(payload.get("uid") or "").strip()
        pid = int(payload.get("pid") or 0)
        if not uid:
            return {"code": 1, "msg": "缺少 uid"}
        new_pid = self.persons.link_identity(
            bot_id, platform, uid, pid, str(payload.get("name") or "")
        )
        return {"code": 0, "data": {"pid": new_pid}, "msg": "已绑定"}


    async def api_person_unlink(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        platform = str(payload.get("platform") or "").strip()
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            return {"code": 1, "msg": "缺少 uid"}
        ok = self.persons.unlink_identity(bot_id, platform, uid)
        return {"code": 0 if ok else 1, "data": {"ok": ok}, "msg": "已解绑" if ok else "没找到这个身份"}


    async def api_person_merge(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        keep_pid = int(payload.get("keep_pid") or 0)
        drop_pid = int(payload.get("drop_pid") or 0)
        if not keep_pid or not drop_pid or keep_pid == drop_pid:
            return {"code": 1, "msg": "keep_pid 和 drop_pid 必须是两个不同的人物"}
        ok = self.persons.merge_persons(bot_id, keep_pid, drop_pid)
        return {"code": 0 if ok else 1, "data": {"ok": ok}, "msg": "已合并" if ok else "合并失败"}


    async def api_memories(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        uid = str(payload.get("uid") or "").strip()
        query = str(payload.get("q") or "").strip()
        if query:
            # recall 开了语义召回会走 HTTP（embedding）；面板是 async 处理函数，
            # 同步调用会卡住事件循环，最多 embedding_timeout 秒。丢线程池里跑。
            items = await asyncio.to_thread(self.retrieval.recall, bot_id, uid, query, 100) if uid else []
        else:
            items = self.memory.list_memories(bot_id, uid, 200)
        return {"code": 0, "data": {"items": items}}


    async def api_memory_save(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        uid = str(payload.get("uid") or "").strip()
        content = str(payload.get("content") or "").strip()
        if not uid or not content:
            return {"code": 1, "msg": "uid 和 content 不能为空"}
        result = self.memory.remember(
            bot_id, uid, content,
            kind=str(payload.get("kind") or "manual"),
            importance=payload.get("importance", 0.6),
            scope=str(payload.get("scope") or "private"),
            group_id=str(payload.get("group_id") or ""),
            platform=str(payload.get("platform") or ""),
        )
        return {"code": 0 if result.get("ok") else 1, "data": result, "msg": "已记住"}


    async def api_memory_backfill_platform(self):
        """一次性回填：给 platform 为空的老记忆补来源平台（只补能确定的）。"""
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        apply = payload.get("apply", True)
        if isinstance(apply, str):
            apply = apply.strip().lower() not in ("0", "false", "no", "off")
        payload_apply = bool(apply)
        try:
            result = await asyncio.to_thread(
                self.memory.backfill_memory_platform, bot_id, 600, 20000, payload_apply,
            )
        except Exception as exc:
            return {"code": 1, "msg": str(exc)}
        return {"code": 0, "data": result, "applied": payload_apply}


    async def api_memory_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        try:
            memory_id = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            memory_id = 0
        if not memory_id:
            return {"code": 1, "msg": "缺少 id"}
        return {"code": 0, "data": {"deleted": self.memory.delete_memory(bot_id, memory_id)}}


    async def api_backup_now(self):
        payload = await self._body()
        keep = payload.get("keep", self._cfg_int("backup_keep", 7))
        path = await self.mem.backup_now(int(keep))
        if not path:
            return {"code": 1, "msg": "备份失败"}
        return {"code": 0, "data": {"path": path, "name": os.path.basename(path),
                                   "items": await self.mem.list_backups()}}


    async def api_backup_list(self):
        return {"code": 0, "data": {"items": await self.mem.list_backups()}}


    async def api_backup_export(self):
        payload = await self._body()
        snapshot = await self.mem.export_snapshot(payload.get("bot_id") or "")
        return {"code": 0, "data": {"snapshot": snapshot}}


    async def api_backup_import(self):
        payload = await self._body()
        snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
        if not isinstance(snapshot, dict):
            return {"code": 1, "msg": "缺少 snapshot"}
        counts = await self.mem.import_snapshot(snapshot)
        return {"code": 0, "data": counts}


    async def api_backup_restore(self):
        payload = await self._body()
        name = str(payload.get("name") or "").strip()
        if not name:
            return {"code": 1, "msg": "缺少备份文件名"}
        counts = await self.mem.restore_backup(name, payload.get("bot_id") or "")
        return {"code": 0, "data": counts}


    async def api_relations(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        uid = str(payload.get("uid") or "").strip()
        items = self.persons.list_relations(bot_id, uid)
        # 面板上直接显名字，不要只给一串 uid
        names = {}
        profiles = []
        try:
            profiles = self.profiles.list_profiles(bot_id, "", 500)
            for row in profiles:
                key = str(row.get("uid") or "")
                if key:
                    names[key] = str(row.get("name") or row.get("alias") or "")
        except Exception:
            names = {}
        names.setdefault("bot", "你")

        # 画像里显式设过的关系也进图：不然把对方设成「恋人」，
        # 图上还停在自动沉淀出来的「熟人」。
        profile_rel = {}
        for row in profiles:
            key = str(row.get("uid") or "")
            rel = str(row.get("relation") or "").strip()
            if key and rel and rel not in ("普通",):
                profile_rel[key] = rel
        if profile_rel:
            pairs = {}
            for pair_item in items:
                pairs[frozenset((str(pair_item.get("a_uid") or ""), str(pair_item.get("b_uid") or "")))] = pair_item
            for key, rel in profile_rel.items():
                if key in ("bot", "self"):
                    continue
                item = pairs.get(frozenset(("bot", key)))
                if item is not None:
                    item["relation"] = rel
                    item["note"] = "画像：关系字段"
                    item["profile_relation"] = rel
                else:
                    items.append({
                        "a_uid": "bot", "b_uid": key, "relation": rel,
                        "note": "画像：关系字段", "profile_relation": rel,
                        "a_name": names.get("bot"), "b_name": names.get(key) or key,
                    })

        for item in items:
            for side in ("a", "b"):
                key = str(item.get(side + "_uid") or "")
                # 画像里没有就保留 store 用人物卡算出来的名字（别覆盖成空）
                if names.get(key):
                    item[side + "_name"] = names[key]
        return {"code": 0, "data": {"items": items}}


    async def api_relation_save(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        a_uid = str(payload.get("a_uid") or "").strip()
        b_uid = str(payload.get("b_uid") or "").strip()
        if not a_uid or not b_uid:
            return {"code": 1, "msg": "需要两个 uid"}
        ok = self.persons.save_relation(
            bot_id, a_uid, b_uid,
            str(payload.get("relation") or "朋友"),
            str(payload.get("note") or ""),
        )
        return {"code": 0 if ok else 1, "msg": "已保存" if ok else "保存失败"}


    async def api_relation_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        a_uid = str(payload.get("a_uid") or "").strip()
        b_uid = str(payload.get("b_uid") or "").strip()
        if not a_uid or not b_uid:
            return {"code": 1, "msg": "需要两个 uid"}
        ok = self.persons.delete_relation(bot_id, a_uid, b_uid)
        return {"code": 0 if ok else 1, "msg": "已删除" if ok else "没有这条关系"}


    async def api_groups(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        return {"code": 0, "data": {
            "items": self.messages.group_sessions(bot_id, 500),
            "names": self.messages.group_name_overrides(bot_id),
        }}


    async def api_group_rename(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        umo = str(payload.get("umo") or "").strip()
        name = str(payload.get("name") or "").strip()
        if not umo:
            return {"code": 1, "msg": "缺少 umo"}
        ok = self.messages.rename_group(bot_id, umo, name)
        return {"code": 0 if ok else 1, "msg": "已保存群名" if ok else "名字不能为空"}


    async def api_messages(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        umo = str(payload.get("umo") or "").strip()
        limit = payload.get("limit", 200)
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        platform = str(payload.get("platform") or "").strip()
        uid = str(payload.get("uid") or "").strip()
        kind = str(payload.get("kind") or "").strip().lower()
        query = str(payload.get("query") or "").strip()
        if query:
            # 带时间说法（昨天下午 / 上周三 / 11月6号晚上10点）就走分层检索：
            # 年 → 月日 → 小时 → 具体消息，正文不带时间戳
            result = await self.mem.recall_messages(
                bot_id, query=query, umo=umo, uid=uid, limit=limit
            )
            return {"code": 0, "data": result}
        try:
            summary_limit = max(20, min(200, int(limit)))
        except (TypeError, ValueError):
            summary_limit = 100
        return {"code": 0, "data": {
            "items": self.messages.list_messages_grouped(
                bot_id, umo, limit, pid=pid, platform=platform, uid=uid,
                summary_limit=summary_limit, kind=kind,
            )
        }}


    async def api_message_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        try:
            message_id = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            message_id = 0
        if not message_id:
            return {"code": 1, "msg": "缺少 id"}
        return {"code": 0, "data": {"deleted": self.messages.delete_message(bot_id, message_id)}}


    async def api_message_clear(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        umo = str(payload.get("umo") or "").strip()
        if str(payload.get("confirm") or "") not in ("1", "true", "yes"):
            return {"code": 1, "msg": "清空需要确认：confirm=1"}
        return {"code": 0, "data": {"deleted": self.messages.clear_messages(bot_id, umo)}}


    async def api_logs(self):
        payload = await self._body()
        bot_id = self._bot_id(payload)
        level = str(payload.get("level") or "").strip()
        limit = payload.get("limit", 200)
        return {"code": 0, "data": {"items": self.logs.list_logs(bot_id, level, limit)}}


    async def api_log_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        try:
            log_id = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            log_id = 0
        if not log_id:
            return {"code": 1, "msg": "缺少 id"}
        return {"code": 0, "data": {"deleted": self.logs.delete_log(bot_id, log_id)}}


    async def api_log_clear(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        if str(payload.get("confirm") or "") not in ("1", "true", "yes"):
            return {"code": 1, "msg": "清空需要确认：confirm=1"}
        return {"code": 0, "data": {"deleted": self.logs.clear_logs(bot_id)}}


    async def api_schedule(self):
        """日程/世界观这类缓存：列出 key 和大小，点进去看内容。"""
        payload = await self._body()
        bot_id = self._bot_id(payload)
        key = str(payload.get("key") or "").strip()
        if key:
            value = self.cache.cache_get(bot_id, key)
            return {"code": 0, "data": {"key": key, "value": value}}
        items = []
        for name in self.cache.cache_keys(bot_id):
            value = self.cache.cache_get(bot_id, name)
            size = 0
            try:
                size = len(json.dumps(value, ensure_ascii=False))
            except Exception:
                size = 0
            items.append({"key": name, "size": size})
        return {"code": 0, "data": {"items": items}}


    async def api_schedule_delete(self):
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        key = str(payload.get("key") or "").strip()
        if not key:
            return {"code": 1, "msg": "缺少 key"}
        return {"code": 0, "data": {"deleted": self.cache.cache_delete(bot_id, key)}}


    async def api_maintenance(self):
        """手动跑一次保留策略：压缩老聊天 + 删过期日志。"""
        payload = await self._body()
        bot_id = self._bot_id(payload, write=True)
        result: dict[str, Any] = {}
        try:
            result.update(self.messages.compress_messages(bot_id, self.compress_after, self.chat_days))
        except Exception as exc:
            result["compress_error"] = str(exc)
        try:
            result["logs_purged"] = self.logs.purge_logs(bot_id, self.log_days)
        except Exception as exc:
            result["log_error"] = str(exc)
        return {"code": 0, "data": result}

