# -*- coding: utf-8 -*-
"""core/api.py —— 跨插件唯一门面 MemoryAPI（插件实例上的 self.mem）。

守护插件、陪伴插件、脚本只认这一层；内部一律转发 services，不写 SQL、不做打分。

44 个方法（名称 / 位置参数 / 默认值 / 返回形状都不能变）：
    画像  get_profile / save_profile / list_profiles / delete_profile
    记忆  remember / recall / list_memories / delete_memory
    夜间  nightly_memory_candidates / nightly_apply（守护插件 03:00 提炼用）
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
import hashlib

try:  # 线上走 AstrBot 日志
    from astrbot.api import logger
except Exception:  # pragma: no cover - 离线单测
    class _NullLogger:
        def __getattr__(self, name):
            def _noop(*args, **kwargs):
                return None
            return _noop

    logger = _NullLogger()

try:  # 包模式
    from ..services import recall_rerank, semantic_consolidation
except ImportError:  # 顶层导入（测试/脚本）
    try:
        from services import recall_rerank, semantic_consolidation
    except ImportError:  # 只装 api 的单测：重排/语义巩固关掉即可
        recall_rerank = None  # type: ignore[assignment]
        semantic_consolidation = None  # type: ignore[assignment]

DEFAULT_BOT_ID = "default"

#: 跨插件记忆 API 协议（Phase 19）：守护只读 protocol_info() 判断兼容性。
PROTOCOL_NAME = "amo-memory"
PROTOCOL_VERSION = "1.0"


def memory_api_names():
    """MemoryAPI 公开方法名（排序后作为 api_hash 的输入，只读）。

    protocol_info 自身不计入：指纹描述的是「原有记忆 API 表面」。
    """
    return sorted(name for name in dir(MemoryAPI)
                  if not name.startswith("_") and name != "protocol_info")


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
                       platform: str = "", tags: str = "") -> dict:
        try:
            refresh = getattr(self._p, "refresh_config", None)
            if callable(refresh):
                refresh()
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
                platform=platform, tags=tags,
            )
        except Exception as exc:
            logger.debug("[amo_data] 写入记忆失败: %s" % exc)
            return {"ok": False, "reason": str(exc)}

    async def consolidate_semantic(self, bot_id: str) -> dict:
        """跨天同主题语义巩固：聚类 → 一次小模型合成一句 → 覆盖 keep、其余 merged。"""
        if semantic_consolidation is None:
            return {"ok": False, "reason": "module_missing"}
        if not self._p.settings.get("semantic_consolidation_enabled", True):
            return {"ok": False, "reason": "disabled"}
        provider = None
        try:
            provider = self._p.context.get_using_provider()
        except Exception as exc:
            logger.debug("[amo_data] 取语义巩固 provider 失败: %s" % exc)
        if provider is None:
            return {"ok": False, "reason": "no_provider"}
        bot = self._bot(bot_id)
        groups = await asyncio.to_thread(self._p.memory.semantic_clusters, bot)
        if not groups:
            return {"ok": True, "groups": 0, "merged": 0}
        cap = max(1, int(self._p.settings.get("semantic_consolidation_max_groups", 3) or 3))
        timeout = max(1.0, float(self._p.settings.get("semantic_consolidation_timeout", 20) or 20))
        merged_total = 0
        done = 0
        for group in groups[:cap]:
            prompt = semantic_consolidation.build_prompt(group["contents"])
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=prompt, system_prompt="只输出合并后的那一句话。"),
                    timeout=timeout,
                )
            except Exception as exc:
                logger.debug("[amo_data] 语义巩固调用失败: %s" % exc)
                continue
            merged = str(getattr(resp, "completion_text", "") or "").strip()
            if not semantic_consolidation.accept_merged(merged, group["contents"]):
                continue
            merged_total += await asyncio.to_thread(
                self._p.memory.apply_semantic_merge,
                int(group["keep_id"]), merged, list(group["ids"]),
            )
            done += 1
        return {"ok": True, "groups": len(groups), "done": done, "merged": merged_total}

    async def _maybe_rerank(self, items: list, query: str, limit: int) -> list:
        """可选二阶段重排：只有开了配置、且能拿到当前 provider 才做；失败原样返回。"""
        if not items or recall_rerank is None:
            return items
        if not self._p.settings.get("recall_rerank_enabled", False):
            return items
        provider = None
        try:
            provider = self._p.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None:
            return items

        async def _call(prompt: str) -> str:
            resp = await provider.text_chat(
                prompt=prompt, system_prompt="只输出 JSON，不要解释。"
            )
            return str(getattr(resp, "completion_text", "") or "")

        picked = await recall_rerank.rerank(
            items, query, limit, _call,
            max_candidates=int(self._p.settings.get("recall_rerank_candidates", 20) or 20),
            timeout=float(self._p.settings.get("recall_rerank_timeout", 8.0) or 8.0),
        )
        return picked[:max(1, int(limit))]

    async def recall(self, bot_id: str, uid: str, query: str = "", limit: int = 6,
                     scope: str = "", group_id: str = "", platform: str = "",
                     pid: int = 0, tags: str = "") -> list:
        try:
            refresh = getattr(self._p, "refresh_config", None)
            if callable(refresh):
                refresh()
            # recall 里有 BM25 打分（几千条记忆时几十毫秒）和可选的 embedding 网络请求，
            # 放线程里跑，别把 AstrBot 的事件循环卡住（卡住=所有聊天都排队）
            # tags 非空时只召回带这个标签的记忆（如「#活动 event:<id>」）。
            items = await asyncio.to_thread(
                self._p.retrieval.recall,
                self._bot(bot_id), uid, query,
                limit=limit, scope=scope, group_id=group_id,
                platform=platform, pid=pid, tags=tags,
            )
            return await self._maybe_rerank(items, query, limit)
        except Exception as exc:
            logger.debug("[amo_data] 召回记忆失败: %s" % exc)
            return []

    async def correct(self, bot_id: str, uid: str, hint: str, right_content: str,
                      scope: str = "private", group_id: str = "",
                      platform: str = "") -> dict:
        """他说记错了：旧记忆标 superseded，再写正确版本（写失败则一条都不动）。"""
        try:
            return await asyncio.to_thread(
                self._p.memory.correct, self._bot(bot_id), uid, hint, right_content,
                scope=scope, group_id=group_id, platform=platform,
            )
        except Exception as exc:
            logger.debug("[amo_data] 纠正记忆失败: %s" % exc)
            return {"ok": False, "reason": str(exc)}

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

    # ---------- 夜间提炼（守护插件「我能永远守护你」03:00 调） ----------
    async def nightly_memory_candidates(self, hours: int = 24, limit: int = 300) -> list:
        """近 N 小时写入的流水记忆（只读），SQL 在后台线程里跑。"""
        # 读失败必须让调用方知道；返回 [] 会被守护当成「今天没有流水」而封盘。
        return await asyncio.to_thread(
            self._p.memory.nightly_candidates, hours=hours, limit=limit
        )

    async def nightly_apply(self, meta: dict, facts: list, ids: list) -> dict:
        """提炼出的事实写回长期记忆，并归档对应原流水；一条都没写进去就不归档。"""
        info = meta or {}
        bot_id = self._bot(info.get("bot_id") or "")
        uid = str(info.get("uid") or "")
        scope = str(info.get("scope") or "private")
        group_id = str(info.get("group_id") or "")
        subject = str(info.get("subject") or "user")
        written = 0
        absorbed = set()
        for fact in (facts or []):
            text = str(fact or "").strip()
            if not text:
                continue
            try:
                out = await asyncio.to_thread(
                    self._p.memory.remember, bot_id, uid, text,
                    kind="fact", importance=0.8, scope=scope,
                    group_id=group_id, subject=subject,
                )
            except Exception as exc:
                logger.debug("[amo_data] 夜间记忆写回失败: %s" % exc)
                continue
            if not isinstance(out, dict) or out.get("ok", True) is not False:
                written += 1
                # 提炼出的事实和某条流水几乎一样时会合并进那一行：
                # 这行已经被改写成事实，就不能再跟着流水一起归档，否则事实也跟着消失。
                try:
                    merged_id = int(out.get("memory_id") or 0)
                except (TypeError, ValueError, AttributeError):
                    merged_id = 0
                if merged_id > 0:
                    absorbed.add(merged_id)
        archived = 0
        if written:
            keep = []
            for value in (ids or []):
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number > 0 and number not in absorbed:
                    keep.append(number)
            try:
                # 只归档属于 meta 里这个 bot/uid 的行：ids 来自调用方，不能全信
                archived = await asyncio.to_thread(
                    self._p.memory.nightly_archive, keep, bot_id, uid
                )
            except Exception as exc:
                logger.debug("[amo_data] 夜间记忆归档失败: %s" % exc)
        return {"written": written, "archived": int(archived or 0)}

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

        返回 **dict**（messages/span/path/label），能陪的【按时间回看】吃的就是这个；
        面板 ``api_messages`` 用的是 ``services/message_engine.recall_messages``（返回 list）。
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
            # 周度整理是同步重活（摘要拆分 + 近重复两两比对）：放线程里跑，
            # 别卡住事件循环（守护插件和面板都会 await 这个门面）
            return await asyncio.to_thread(
                self._p.compression.compress_memories, self._bot(bot_id), apply=bool(apply)
            )
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

    # ---------- 协议自述（Phase 19） ----------
    def protocol_info(self):
        """只读协议摘要：给守护 compat() 判断跨插件协议版本；不碰库、不联网。"""
        try:  # 包模式优先
            from ..models import db_schema
            from ..utils.config import PLUGIN_VERSION as _version
        except ImportError:  # 测试 / 顶层导入
            from models import db_schema
            from utils.config import PLUGIN_VERSION as _version
        payload = "\n".join(memory_api_names()).encode("utf-8")
        return {
            "plugin": "Astrbot_amoupup_data",
            "protocol": PROTOCOL_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "api_hash": hashlib.sha256(payload).hexdigest()[:12],
            "schema_version": int(getattr(db_schema, "LATEST_VERSION", 0) or 0),
            "version": _version,
        }
