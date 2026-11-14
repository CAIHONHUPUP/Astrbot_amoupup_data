# -*- coding: utf-8 -*-
"""core/api.py —— 跨插件唯一门面 MemoryAPI（插件实例上的 self.mem）。

守护插件、陪伴插件、脚本只认这一层；内部一律转发 services，不写 SQL、不做打分。

39 个方法（名称 / 位置参数 / 默认值 / 返回形状都不能变）：
    画像  get_profile / save_profile / list_profiles / delete_profile
    记忆  remember / recall / list_memories / delete_memory
    关系  list_relations / save_relation / delete_relation
    缓存  cache_get / cache_set / cache_delete
    聊天  add_message / list_messages / delete_message / messages_since /
          messages_between / recall_messages
    整理  compress_memories / archive_stale / maintenance / purge_logs
    备份  export_snapshot / import_snapshot / backup_now / list_backups / restore_backup
    日志  add_log / list_logs
    统计  stats
    人物  resolve_person / person_context / list_persons / person_detail /
          person_link / person_unlink / person_merge

约定：任何异常都不许抛回聊天主链路；按原逻辑返回空值 / False / 错误字典。
"""

from __future__ import annotations

import asyncio

try:  # 线上走 AstrBot 日志
    from astrbot.api import logger
except Exception:  # pragma: no cover - 离线单测
    class _NullLogger:
        def __getattr__(self, name):
            def _noop(*args, **kwargs):
                return None
            return _noop

    logger = _NullLogger()

DEFAULT_BOT_ID = "default"


class MemoryAPI:
    """把 services 包成 async 门面，顺便兜住异常。

    调用方只需要插件实例：await plugin.mem.recall(...)。
    """

    def __init__(self, plugin):
        self._plugin = plugin

    @property
    def _p(self):
        return self._plugin

    @staticmethod
    def _bot(bot_id: str) -> str:
        return str(bot_id or DEFAULT_BOT_ID)

    # ---------- 画像 ----------
    async def get_profile(self, bot_id: str, uid: str) -> dict:
        try:
            return self._p.profiles.get_profile(self._bot(bot_id), uid)
        except Exception as exc:
            logger.debug("[amo_data] 读取画像失败: %s" % exc)
            return {}

    async def save_profile(self, bot_id: str, uid: str, name=None, alias=None,
                           relation=None, remark=None, platform: str = "") -> dict:
        """Amo 改完称呼/关系直接调这个存回去；只传要改的字段即可。

        带上 platform 时顺带把这个身份记进人物表，QQ / 飞书同名会自动并成一个人。
        """
        try:
            profile = self._p.profiles.save_profile(
                self._bot(bot_id), uid,
                name=name, alias=alias, relation=relation, remark=remark,
            )
            if platform:
                display = str(name or profile.get("name") or "")
                self._p.persons.resolve_person(self._bot(bot_id), platform, uid, display)
            return profile
        except Exception as exc:
            logger.debug("[amo_data] 保存画像失败: %s" % exc)
            return {}

    async def list_profiles(self, bot_id: str, keyword: str = "", limit: int = 100) -> list:
        try:
            return self._p.profiles.list_profiles(self._bot(bot_id), keyword, limit)
        except Exception as exc:
            logger.debug("[amo_data] 列画像失败: %s" % exc)
            return []

    async def delete_profile(self, bot_id: str, uid: str) -> dict:
        try:
            return self._p.profiles.delete_profile(self._bot(bot_id), uid)
        except Exception as exc:
            logger.debug("[amo_data] 删除画像失败: %s" % exc)
            return {}

    # ---------- 记忆 ----------
    async def remember(self, bot_id: str, uid: str, content: str, kind: str = "fact",
                       importance: float = 0.5, scope: str = "private",
                       group_id: str = "", idem_key: str = "", subject: str = "user",
                       platform: str = "") -> dict:
        try:
            fn = None
            engine = getattr(self._p, "memory", None)
            if engine is not None:
                try:
                    fn = getattr(engine, "remember", None)
                except Exception:
                    fn = None
            if not callable(fn):
                fn = getattr(self._p, "remember")
            return fn(
                self._bot(bot_id), uid, content,
                kind=kind, importance=importance, scope=scope,
                group_id=group_id, idem_key=idem_key, subject=subject,
                platform=platform,
            )
        except Exception as exc:
            logger.debug("[amo_data] 写入记忆失败: %s" % exc)
            return {"ok": False, "reason": str(exc)}

    async def recall(self, bot_id: str, uid: str, query: str = "", limit: int = 6,
                     scope: str = "", group_id: str = "", platform: str = "",
                     pid: int = 0) -> list:
        try:
            # recall 里有 BM25 打分（几千条记忆时几十毫秒）和可选的 embedding 网络请求，
            # 放线程里跑，别把 AstrBot 的事件循环卡住（卡住=所有聊天都排队）
            return await asyncio.to_thread(
                self._p.retrieval.recall,
                self._bot(bot_id), uid, query,
                limit=limit, scope=scope, group_id=group_id,
                platform=platform, pid=pid,
            )
        except Exception as exc:
            logger.debug("[amo_data] 召回记忆失败: %s" % exc)
            return []

    async def list_memories(self, bot_id: str, uid: str = "", limit: int = 100) -> list:
        try:
            return self._p.memory.list_memories(self._bot(bot_id), uid, limit)
        except Exception as exc:
            logger.debug("[amo_data] 列记忆失败: %s" % exc)
            return []

    async def delete_memory(self, bot_id: str, memory_id: int) -> bool:
        try:
            return self._p.memory.delete_memory(self._bot(bot_id), memory_id)
        except Exception as exc:
            logger.debug("[amo_data] 删除记忆失败: %s" % exc)
            return False

    # ---------- 关系（人物关系网） ----------
    async def list_relations(self, bot_id: str, uid: str = "") -> list:
        try:
            return self._p.persons.list_relations(self._bot(bot_id), uid)
        except Exception as exc:
            logger.debug("[amo_data] 列关系失败: %s" % exc)
            return []

    async def save_relation(self, bot_id: str, a_uid: str, b_uid: str,
                            relation: str, note: str = "") -> bool:
        try:
            return self._p.persons.save_relation(self._bot(bot_id), a_uid, b_uid, relation, note)
        except Exception as exc:
            logger.debug("[amo_data] 保存关系失败: %s" % exc)
            return False

    async def delete_relation(self, bot_id: str, a_uid: str, b_uid: str) -> bool:
        try:
            return self._p.persons.delete_relation(self._bot(bot_id), a_uid, b_uid)
        except Exception as exc:
            logger.debug("[amo_data] 删除关系失败: %s" % exc)
            return False

    # ---------- 缓存 ----------
    async def cache_get(self, bot_id: str, key: str):
        try:
            return self._p.cache.cache_get(self._bot(bot_id), key)
        except Exception:
            return None

    async def cache_set(self, bot_id: str, key: str, value, ttl: int = 0) -> bool:
        try:
            return self._p.cache.cache_set(self._bot(bot_id), key, value, ttl)
        except Exception:
            return False

    async def cache_delete(self, bot_id: str, key: str) -> bool:
        try:
            return self._p.cache.cache_delete(self._bot(bot_id), key)
        except Exception:
            return False

    # ---------- 聊天记录 ----------
    async def add_message(self, bot_id: str, umo: str, uid: str = "", name: str = "",
                          role: str = "user", content: str = "", platform: str = "",
                          message_id: str = "", group_name: str = "") -> int:
        try:
            return self._p.messages.add_message(
                self._bot(bot_id), umo, uid, name, role, content,
                platform=platform, message_id=message_id, group_name=group_name,
            )
        except Exception as exc:
            logger.warning("[amo_data] 写聊天记录失败: %s" % exc)
            return 0

    async def list_messages(self, bot_id: str, umo: str = "", limit: int = 200, uid: str = "") -> list:
        try:
            return self._p.messages.list_messages(self._bot(bot_id), umo, limit, uid=uid)
        except Exception as exc:
            logger.debug("[amo_data] 读聊天记录失败: %s" % exc)
            return []

    async def delete_message(self, bot_id: str, message_id: int) -> bool:
        try:
            return self._p.messages.delete_message(self._bot(bot_id), message_id)
        except Exception:
            return False

    async def messages_since(self, bot_id: str, uid: str, since_ts: int = 0,
                             limit: int = 200, umo: str = "") -> list:
        """增量聊天（里程碑归档用）：某个 uid 或某个会话在 since_ts 之后的消息。"""
        try:
            return self._p.messages.messages_since(self._bot(bot_id), uid, since_ts, limit, umo=umo)
        except Exception as exc:
            logger.debug("[amo_data] 读增量聊天失败: %s" % exc)
            return []

    async def messages_between(self, bot_id: str, start_ts: int, end_ts: int,
                               uid: str = "", limit: int = 2000) -> list:
        """某个时间段内的聊天（陪伴插件写日记用）。"""
        try:
            return self._p.messages.messages_between(
                self._bot(bot_id), start_ts, end_ts, uid, limit
            )
        except Exception as exc:
            logger.debug("[amo_data] 读时间段聊天失败: %s" % exc)
            return []

    async def recall_messages(self, bot_id: str, query: str = "", umo: str = "",
                              uid: str = "", limit: int = 200) -> dict:
        """按时间说法取聊天记录：年 → 月日 → 小时 → 具体消息（四级漏斗）。

        查询里没提时间就退回最近消息；返回的正文**不带时间戳**，
        时间只放在 span 里（调用方要显示日期时自己用）。
        """
        try:
            return self._p.messages.recall_by_time(
                self._bot(bot_id), query, umo=umo, uid=uid, limit=limit,
            )
        except Exception as exc:
            logger.debug("[amo_data] 按时间取聊天记录失败: %s" % exc)
            return {"messages": [], "span": None, "path": {}, "label": ""}

    # ---------- 压缩 / 归档 ----------
    async def compress_memories(self, bot_id: str, apply: bool = True) -> dict:
        """周度整理：合并同天摘要、近重复记忆标过期（不删数据）。"""
        try:
            return self._p.compression.compress_memories(self._bot(bot_id), apply=bool(apply))
        except Exception as exc:
            logger.debug("[amo_data] 记忆压缩失败: %s" % exc)
            return {"merged_summaries": 0, "merged_near_duplicates": 0, "applied": bool(apply)}

    async def archive_stale(self, bot_id: str, days: int = 90,
                            max_importance: float = 0.3, apply: bool = True) -> dict:
        """把长期没被想起的低重要度记忆归档（不删除）。"""
        try:
            return self._p.compression.archive_stale(
                self._bot(bot_id), days=days, max_importance=max_importance, apply=bool(apply)
            )
        except Exception as exc:
            logger.debug("[amo_data] 记忆归档失败: %s" % exc)
            return {"candidate": 0, "archived": 0, "ids": []}

    async def maintenance(self, bot_id: str, chat_days: int = 14, compress_after: int = 3,
                          log_days: int = 7) -> dict:
        """按保留策略压缩/清理：聊天先压缩再删除，日志到期删除。"""
        try:
            return self._p.maintenance.retention(
                self._bot(bot_id), chat_days, compress_after, log_days
            )
        except Exception as exc:
            logger.debug("[amo_data] 压缩聊天失败: %s" % exc)
            return {}

    async def purge_logs(self, bot_id: str, days: int = 7) -> int:
        try:
            return self._p.logs.purge_logs(self._bot(bot_id), days)
        except Exception:
            return 0

    # ---------- 备份 ----------
    def _backup_dir(self) -> str:
        """兼容老代码/脚本：备份目录仍是 plugin_data/<插件>/backups。"""
        return self._p.backup.backup_dir()

    async def export_snapshot(self, bot_id: str = "") -> dict:
        """导出画像 / 记忆 / 关系 / 人物 / 身份别名（不传 bot_id 就导出全部）。"""
        try:
            return self._p.backup.export_snapshot(self._bot(bot_id) if bot_id else "")
        except Exception as exc:
            logger.debug("[amo_data] 导出快照失败: %s" % exc)
            return {}

    async def import_snapshot(self, data: dict) -> dict:
        """合并导入快照（记忆按内容去重，重复导入不会翻倍）。"""
        try:
            return self._p.backup.import_snapshot(data or {})
        except Exception as exc:
            logger.debug("[amo_data] 导入快照失败: %s" % exc)
            return {"error": 1}

    async def backup_now(self, keep: int = 7) -> str:
        try:
            return self._p.backup.backup_now(keep)
        except Exception as exc:
            logger.debug("[amo_data] 数据库备份失败: %s" % exc)
            return ""

    async def list_backups(self) -> list:
        try:
            return self._p.backup.list_backups()
        except Exception:
            return []

    async def restore_backup(self, name: str, bot_id: str = "") -> dict:
        """从 backups/ 里的某个 .db 恢复（合并语义，不覆盖现有数据）。"""
        try:
            return self._p.backup.restore_backup(name, self._bot(bot_id) if bot_id else "")
        except Exception as exc:
            logger.debug("[amo_data] 恢复备份失败: %s" % exc)
            return {"error": 1}

    # ---------- 日志 ----------
    async def add_log(self, bot_id: str, level: str, tag: str, content: str) -> int:
        try:
            return self._p.logs.add_log(self._bot(bot_id), level, tag, content)
        except Exception:
            return 0

    async def list_logs(self, bot_id: str, level: str = "", limit: int = 200) -> list:
        try:
            return self._p.logs.list_logs(self._bot(bot_id), level, limit)
        except Exception:
            return []

    # ---------- 统计 ----------
    async def stats(self, bot_id: str) -> dict:
        try:
            return self._p.maintenance.stats(self._bot(bot_id))
        except Exception:
            return {}

    # ---------- 人物：跨平台身份合并 ----------
    async def resolve_person(self, bot_id: str, platform: str, uid: str, name: str = "") -> int:
        """Amo 每次说话前调一次：拿到这个 (平台, uid) 的人物 pid，同名自动并。"""
        try:
            return self._p.persons.resolve_person(self._bot(bot_id), platform, uid, name)
        except Exception as exc:
            logger.debug("[amo_data] 认人失败: %s" % exc)
            return 0

    async def person_context(self, bot_id: str, platform: str, uid: str, limit: int = 8) -> dict:
        """Amo 建提示词时调：这个人在别的平台说过什么，一起带回去。"""
        try:
            return self._p.persons.person_context(self._bot(bot_id), platform, uid, limit)
        except Exception as exc:
            logger.debug("[amo_data] 跨平台上下文失败: %s" % exc)
            return {}

    async def list_persons(self, bot_id: str = "", keyword: str = "", limit: int = 100) -> list:
        try:
            return self._p.persons.list_persons(self._bot(bot_id), keyword, limit)
        except Exception as exc:
            logger.debug("[amo_data] 列人物失败: %s" % exc)
            return []

    async def person_detail(self, bot_id: str, pid: int) -> dict:
        try:
            return self._p.persons.person_detail(self._bot(bot_id), int(pid))
        except Exception:
            return {}

    async def person_link(self, bot_id: str, platform: str, uid: str,
                          pid: int = 0, name: str = "") -> int:
        try:
            return self._p.persons.link_identity(self._bot(bot_id), platform, uid, pid, name)
        except Exception as exc:
            logger.debug("[amo_data] 绑定身份失败: %s" % exc)
            return 0

    async def person_unlink(self, bot_id: str, platform: str, uid: str) -> bool:
        try:
            return self._p.persons.unlink_identity(self._bot(bot_id), platform, uid)
        except Exception:
            return False

    async def person_merge(self, bot_id: str, keep_pid: int, drop_pid: int) -> bool:
        try:
            return self._p.persons.merge_persons(self._bot(bot_id), keep_pid, drop_pid)
        except Exception as exc:
            logger.debug("[amo_data] 合并人物失败: %s" % exc)
            return False
