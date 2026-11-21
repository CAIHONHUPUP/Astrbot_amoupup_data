# -*- coding: utf-8 -*-
"""services/memory_engine.py —— 写记忆：幂等去重、近重复合并、取代、向量、来源回填。

写入前的唯一校验/归一化入口是 services/memory_schema.prepare_write（kind/scope/
importance/idem_key/tags/subject/text），本层不再自己拼一套；SQL 在 models/dao.py。
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

try:
    from . import chain as _chain
except ImportError:  # 顶层导入（测试/脚本）
    from services import chain as _chain

try:
    from ..models.dao import MemoryDao
    from ..services import (entity_graph, entity_tag, fact_dedupe, lifecycle, memory_schema,
                            semantic_consolidation)
    from ..utils.text_process import (
        STOP_BIGRAMS, _clean, _jaccard, _now, _recall_tokens,
        _slot_facts, logger,
    )
except ImportError:  # 顶层导入（测试/脚本）
    from models.dao import MemoryDao
    from services import (entity_graph, entity_tag, fact_dedupe, lifecycle, memory_schema,
                          semantic_consolidation)
    from utils.text_process import (
        STOP_BIGRAMS, _clean, _jaccard, _now, _recall_tokens,
        _slot_facts, logger,
    )


class MemoryEngine:
    """一条记忆的生命周期：写 -> 去重/合并 -> 取代 -> 归档（归档在 compression）。"""

    def __init__(self, db, settings, persons=None):
        self.db = db
        self.settings = settings
        self.persons = persons
        self.dao = MemoryDao(db)
        # 向量回落：Ollama 挂了就别每条查询都等超时
        self._emb_down_until = 0.0
        self._emb_cache: Dict[str, List[float]] = {}
        self._emb_queue: "queue.Queue" = queue.Queue(maxsize=512)
        self._emb_pending: set = set()
        self._emb_pending_lock = threading.Lock()
        self._emb_thread: Optional[threading.Thread] = None
        self._emb_total = 0
        self._emb_failed = 0
        # 人物名字表缓存（写入时打 entity 标签用）：bot_id -> (ts, names)
        self._entity_cache: Dict[str, Any] = {}
        self._emb_warned = False
        if self.setting("embedding_enabled"):
            self._ensure_emb_worker()
            if self.setting("embedding_backfill"):
                try:
                    self.backfill_embeddings(limit=int(self.setting("embedding_backfill_limit", 200)))
                except Exception as exc:
                    logger.debug("[amo_data] optional step failed: %r", exc)

    # ---------- 配置 ----------
    def setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    @property
    def conn(self):
        return self.db.conn

    @property
    def lock(self):
        return self.db.lock

    # ---------- 向量 ----------
    def _ensure_emb_worker(self) -> None:
        if self._emb_thread is not None and self._emb_thread.is_alive():
            return
        self._emb_thread = threading.Thread(
            target=self._emb_worker_loop, name="amo-data-embed", daemon=True
        )
        self._emb_thread.start()

    def _emb_worker_loop(self) -> None:
        while True:
            try:
                item = self._emb_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                return
            memory_id, text = item
            try:
                vec = self._embed(text)
                if vec:
                    self._store_vec(memory_id, vec)
                else:
                    self._emb_failed += 1
            except Exception:
                self._emb_failed += 1
            finally:
                with self._emb_pending_lock:
                    self._emb_pending.discard(int(memory_id))

    def _lifecycle_overrides(self) -> Dict[str, Any]:
        """面板配置 → lifecycle.plan 的覆盖项（0/空 = 不覆盖，用内置表）。"""
        def _num(key, default):
            try:
                value = float(self.setting(key, default) or 0)
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None
        return {
            "noise_days": _num("lifecycle_noise_days", 30),
            "summary_days": _num("lifecycle_summary_days", 180),
            "expire_days": _num("lifecycle_expire_days", 365),
        }

    def preview_lifecycle(self, bot_id: str) -> Dict[str, Any]:
        """dry-run：按当前 TTL 算会休眠/过期多少条，取样给面板看。"""
        rows = [dict(row) for row in self.dao.lifecycle_rows(bot_id, 5000)]
        dormant, expired = lifecycle.plan(rows, _now(), self._lifecycle_overrides())
        by_id = {int(row["id"]): row for row in rows}
        sample = []
        for memory_id in (dormant + expired)[:5]:
            row = by_id.get(int(memory_id)) or {}
            sample.append({
                "id": int(memory_id),
                "kind": str(row.get("kind") or ""),
                "days": round((_now() - int(row.get("last_hit_at") or row.get("created_at") or _now())) / 86400.0, 1),
                "content": str(row.get("content") or "")[:60],
            })
        return {"candidates": len(rows), "dormant": len(dormant), "expired": len(expired),
                "sample": sample, "settings": self._lifecycle_overrides(),
                "counts": self.dao.status_counts(bot_id)}

    def semantic_clusters(self, bot_id: str, limit: int = 2000) -> List[Dict[str, Any]]:
        """跨天同主题候选簇（纯规则聚类，不调模型）。"""
        kinds = sorted(semantic_consolidation.MERGEABLE_KINDS)
        marks = ",".join("?" for _ in kinds)
        with self.dao.lock:
            rows = self.dao.conn.execute(
                "SELECT id, uid, scope, group_id, kind, content, importance, created_at "
                "FROM memories WHERE bot_id=? AND COALESCE(status,'active')='active' "
                "AND kind IN (" + marks + ") ORDER BY id DESC LIMIT ?",
                [str(bot_id)] + kinds + [max(1, int(limit))],
            ).fetchall()
        return semantic_consolidation.cluster([dict(row) for row in rows])

    def apply_semantic_merge(self, keep_id: int, merged: str, ids: List[int]) -> int:
        """把一簇合成的那句话写回 keep，其余标 merged（不删正文）。"""
        others = [int(i) for i in ids if int(i) != int(keep_id)]
        if not others:
            return 0
        row = self.dao.conn.execute(
            "SELECT MAX(importance) AS top FROM memories WHERE id IN (" + ",".join("?" for _ in ids) + ")",
            [int(i) for i in ids],
        ).fetchone()
        importance = float(row["top"] or 0.6) if row else 0.6
        self.dao.merge_summary(int(keep_id), str(merged), len(others), importance)
        self.dao.mark_merged(int(keep_id), others)
        return len(others)

    def run_lifecycle(self, bot_id: str, apply: bool = True) -> Dict[str, Any]:
        """按 kind 的 TTL 把旧流水标休眠/过期；开了清理就把过期且久未想起的删掉。"""
        rows = [dict(row) for row in self.dao.lifecycle_rows(bot_id, 5000)]
        dormant, expired = lifecycle.plan(rows, _now(), self._lifecycle_overrides())
        purged = 0
        if apply:
            if dormant:
                self.dao.set_status(dormant, "dormant", bot_id)
            if expired:
                self.dao.set_status(expired, "expired", bot_id)
            try:
                keep_days = float(self.setting("lifecycle_purge_expired_days", 0) or 0)
            except (TypeError, ValueError):
                keep_days = 0.0
            if keep_days > 0:
                purged = self.dao.purge_expired(bot_id, int(_now() - keep_days * 86400))
        return {"candidates": len(rows), "dormant": len(dormant), "expired": len(expired),
                "purged": purged}

    def _entity_names(self, bot_id: str) -> List[str]:
        """已知人物名字（persons + person_links），缓存 5 分钟，别每条写入都查库。"""
        key = str(bot_id)
        now = time.time()
        cached = self._entity_cache.get(key)
        if cached and now - float(cached[0]) < 300.0:
            return list(cached[1])
        names: List[str] = []
        try:
            with self.dao.lock:
                rows = self.dao.conn.execute(
                    "SELECT name FROM persons WHERE bot_id=? AND COALESCE(name,'')<>'' "
                    "UNION SELECT name FROM person_links WHERE bot_id=? "
                    "AND COALESCE(name,'')<>''",
                    (key, key),
                ).fetchall()
            names = [str(row["name"]).strip() for row in rows if str(row["name"]).strip()]
        except Exception:
            names = []
        self._entity_cache[key] = (now, names)
        return names

    def _entity_tags(self, bot_id: str, text: str, tags: str) -> str:
        try:
            return entity_tag.apply_entity_tags(text, tags, self._entity_names(bot_id))
        except Exception:
            return tags

    def _enqueue_embedding(self, memory_id: int, text: str) -> None:
        if not self.setting("embedding_enabled") or not memory_id or not text:
            return
        self._ensure_emb_worker()
        mid = int(memory_id)
        with self._emb_pending_lock:
            if mid in self._emb_pending:
                return
            self._emb_pending.add(mid)
        try:
            self._emb_queue.put_nowait((mid, str(text)))
        except queue.Full:
            with self._emb_pending_lock:
                self._emb_pending.discard(mid)

    def _embed(self, text: str) -> List[float]:
        """调 embedding 接口拿向量；失败后 60 秒不再试，避免每条消息都卡超时。"""
        if not self.setting("embedding_enabled"):
            return []
        if time.time() < self._emb_down_until:
            return []
        base = str(self.setting("embedding_base", "") or "").rstrip("/")
        model = str(self.setting("embedding_model", "") or "")
        if not base or not model:
            return []
        url = base if base.endswith("/embeddings") else base + "/v1/embeddings"
        payload = json.dumps({"model": model, "input": str(text or "")[:2000]}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            timeout = float(self.setting("embedding_timeout", 3.0) or 3.0)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            items = data.get("data") or []
            vec = items[0].get("embedding") if items else None
            if isinstance(vec, list) and vec:
                self._emb_total += 1
                return [float(x) for x in vec]
        except Exception:
            self._emb_down_until = time.time() + 60.0
            # 只提醒一次：没装 embedding 服务的人，否则每次失败都刷屏。
            if not self._emb_warned:
                self._emb_warned = True
                try:
                    logger.warning(
                        "[能记] 连不上 embedding 服务 %s（%s）：语义召回已退回关键词，"
                        "记忆读写不受影响；装好服务或关掉 embedding_enabled 即可" % (url, model)
                    )
                except Exception as exc:
                    logger.debug("[amo_data] optional step failed: %r", exc)
        return []

    def _embed_cached(self, text: str) -> List[float]:
        key = str(text or "")[:200]
        if key in self._emb_cache:
            return self._emb_cache[key]
        vec = self._embed(key)
        if vec:
            if len(self._emb_cache) > 128:
                self._emb_cache.clear()
            self._emb_cache[key] = vec
        return vec

    def _store_vec(self, memory_id: int, vec: List[float]) -> None:
        try:
            self.dao.store_vec(
                int(memory_id), str(self.setting("embedding_model", "") or ""), vec
            )
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)

    def _load_vecs(self, ids: List[int]) -> Dict[int, List[float]]:
        return self.dao.load_vecs(ids)

    def correct(self, bot_id: str, uid: str, hint: str, right_content: str,
                scope: str = "private", group_id: str = "",
                platform: str = "") -> Dict[str, Any]:
        """他说「记错了」：把 hint 命中的旧记忆标 superseded，再写正确版本。

        先写新的（idem_key 用内容指纹，绕开近似去重），写成功才动旧的：
        写失败就一条都不改，避免把记忆删成空白。
        """
        hint_text = str(hint or "").strip()
        right = str(right_content or "").strip()
        if len(hint_text) < 2 or len(right) < 2:
            return {"ok": False, "reason": "hint 和正确内容都要至少 2 个字"}
        try:
            from ..utils.text_process import _norm_key
        except ImportError:
            from utils.text_process import _norm_key
        saved = self.remember(
            bot_id, uid, right, kind="fact", importance=0.85, scope=scope,
            group_id=group_id, platform=platform, idem_key=_norm_key(right),
        )
        if not saved.get("ok"):
            return {"ok": False, "reason": saved.get("reason") or "写入失败"}
        new_id = int(saved.get("memory_id") or 0)
        old_ids = self.dao.find_active_like(bot_id, uid, hint_text, exclude=new_id, limit=5)
        if old_ids:
            self.dao.supersede_many(old_ids, new_id)
        # 写路径本身也会做负状态覆盖，所以统一数「指向新 id 的失效条数」
        superseded = self.dao.count_superseded_by(new_id)
        return {"ok": True, "memory_id": new_id, "superseded": superseded,
                "superseded_ids": old_ids}

    def backfill_embeddings(self, limit: int = 200) -> int:
        """给还没有向量的记忆排队补向量（交给后台线程，不阻塞聊天）。"""
        if not self.setting("embedding_enabled"):
            return 0
        try:
            rows = self.dao.missing_vecs(limit)
        except Exception:
            return 0
        for row in rows:
            self._enqueue_embedding(int(row["id"]), str(row["content"] or ""))
        return len(rows)

    def embedding_stats(self) -> Dict[str, Any]:
        stats = self.dao.vec_stats()
        return {
            "enabled": bool(self.setting("embedding_enabled")),
            "model": str(self.setting("embedding_model", "") or ""),
            "vectors": stats["vectors"],
            "memories": stats["memories"],
            "embedded_ok": self._emb_total,
            "failed": self._emb_failed,
        }

    def close(self) -> None:
        try:
            self._emb_queue.put_nowait(None)
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)

    # ---------- 写 ----------
    def remember(self, bot_id: str, uid: str, content: str, kind: str = "fact",
                 importance: float = 0.5, scope: str = "private", group_id: str = "",
                 subject: str = "user", platform: str = "", idem_key: str = "",
                 created_at: Optional[int] = None, last_hit_at: Optional[int] = None,
                 hit_count: int = 0, keep_existing_content: bool = False,
                 tags: str = "") -> Dict[str, Any]:
        now = _now()
        trace = _chain.new_chain("remember", bool(self.setting("chain_trace", True)))
        prepared = memory_schema.prepare_write(
            content=content, kind=kind, importance=importance, scope=scope,
            group_id=group_id, subject=subject, idem_key=idem_key, tags=tags,
            auto_dedupe=bool(self.setting("auto_dedupe", True)),
            created_at=created_at, last_hit_at=last_hit_at, hit_count=hit_count,
            now=now,
        )
        if not prepared["ok"]:
            trace.fail("prepare", prepared["reason"])
            return {"ok": False, "reason": prepared["reason"]}
        trace.step("prepare", "kind=%s" % prepared["kind"])
        text = prepared["text"]
        kind = prepared["kind"]
        static = prepared["static"]
        fact = prepared["fact"]
        importance = prepared["importance"]
        scope = prepared["scope"]
        group_id = prepared["group_id"]
        subject = prepared["subject"]
        idem_key = prepared["idem_key"]
        tags = prepared["tags"]
        if self.setting("recall_entity_tags", True):
            tags = self._entity_tags(bot_id, text, tags)
        created_at = prepared["created_at"]
        last_hit_at = prepared["last_hit_at"]
        hit_count = prepared["hit_count"]
        bot_id, uid = str(bot_id), str(uid)
        platform = str(platform or "").strip()
        pid = 0
        try:
            if self.persons is not None:
                if platform:
                    # 带平台来的写入：先把身份登记进人物表（写前强制 resolve），
                    # 再取主 uid；这样 QQ / 飞书说的是同一个人，pid 一致
                    pid = self.persons.resolve_person(bot_id, platform, uid, "")
                    uid = self.persons.canonical_uid(bot_id, platform, uid) or uid
                else:
                    uid = self.persons.canonical_uid_for_uid(bot_id, uid) or uid
                    pid = self.persons.pid_for_uid(bot_id, uid)
        except Exception:
            pid = 0

        merged = _try_merge(self, bot_id, uid, scope, group_id, text, importance, now,
                            subject, fact, static, keep_existing_content, idem_key, tags)
        if merged is not None:
            trace.step("merge", "id=%s" % merged.get("memory_id"))
            trace.done()
            return merged

        try:
            memory_id = self.dao.insert(
                bot_id, uid, pid, platform, scope, group_id, _clean(kind, 32) or "fact",
                subject, text, importance, hit_count, created_at, last_hit_at, idem_key, tags,
            )
        except sqlite3.IntegrityError:
            # 竞态：同一 idem 刚被别的线程写进来，唯一索引挡住了 INSERT。回头再走一次
            # 合并/复活路径，绝不能把这条记忆静默丢掉（旧逻辑会让整条写入失败）。
            merged = _try_merge(self, bot_id, uid, scope, group_id, text, importance, now,
                                subject, fact, static, keep_existing_content, idem_key, tags)
            if merged is not None:
                trace.step("merge_race", "id=%s" % merged.get("memory_id"))
                trace.done()
                return merged
            raise
        if self.setting("identity_debug", True):
            logger.info(
                "[MEMORY_DEBUG] person_id=%s source_platform=%s hit=%s reason=write:%s",
                pid, platform or "unknown", 0, str(kind or "fact"),
            )
        trace.step("insert", "id=%d" % memory_id)
        self._enqueue_embedding(memory_id, text)
        trace.step("vector_queue")
        if self.setting("recall_entity_graph", True):
            try:
                entity_graph.link_memory(self.dao, bot_id, memory_id, text,
                                         self._entity_names(bot_id))
                trace.step("graph")
            except Exception as exc:  # 图谱是增强项：失败不影响这条记忆本身
                logger.debug("[amo_data] 实体图谱写入失败: %r", exc)
        try:
            superseded = self._detect_supersede(bot_id, uid, scope, group_id, memory_id, text)
        except Exception:
            superseded = []
        try:
            expired = self._detect_fact_reversal(
                bot_id, uid, scope, group_id, memory_id, text, static)
        except Exception:
            expired = []
        return {"ok": True, "memory_id": memory_id, "duplicate": False,
                "created_at": created_at, "superseded": superseded, "expired": expired}

    def _near_duplicate(self, bot_id: str, uid: str, scope: str, group_id: str,
                        text: str):
        """在同一个人的最近 200 条里找二元组 Jaccard 超过阈值的近重复。"""
        try:
            ratio = float(self.setting("near_duplicate_ratio", 0.92) or 0.92)
        except (TypeError, ValueError):
            ratio = 0.92
        if ratio <= 0 or ratio > 1:
            return None
        tokens = _recall_tokens(text)
        if len(tokens) < 6:
            return None
        rows = self.dao.recent(bot_id, uid, scope, group_id, 200)
        for row in rows:
            other = _recall_tokens(str(row["content"] or ""))
            if len(other) < 6:
                continue
            if _jaccard(tokens, other) >= ratio:
                return row
        return None

    def _detect_fact_reversal(self, bot_id: str, uid: str, scope: str, group_id: str,
                              new_id: int, text: str, static: bool) -> List[int]:
        """偏好/状态反转（“不爱吃了/改喝热的”）：旧记忆标 expired，不物理删除。"""
        if not static:
            return []
        rows = self.dao.recent(bot_id, uid, scope, group_id, 200)
        old_ids: List[int] = []
        for row in rows:
            if int(row["id"]) == int(new_id):
                continue
            old_text = str(row["content"] or "")
            if fact_dedupe.should_expire(old_text, text):
                old_ids.append(int(row["id"]))
                continue
            old_key = fact_dedupe.fact_key(old_text)
            if old_key and old_key == fact_dedupe.fact_key(text):
                old_ids.append(int(row["id"]))
        if old_ids:
            self.dao.supersede(int(new_id), old_ids)
            logger.info("[MEMORY] 状态反转：%d 条旧记忆标 expired（新：%s）",
                        len(old_ids), text[:40])
        return old_ids

    def _detect_supersede(self, bot_id: str, uid: str, scope: str, group_id: str,
                          new_id: int, text: str) -> List[int]:
        """新记忆和旧记忆在同一个"会变的槽位"上取值不同 → 旧的标 superseded。

        只处理明确陈述（住哪 / 在哪上班 / 生日 / 职业），不做语义推理；
        要求两条内容有一定词汇重叠（Jaccard>=0.2），避免把无关话题误判成冲突。
        """
        new_slots = _slot_facts(text)
        if not new_slots:
            return []
        new_tokens = _recall_tokens(text)
        rows = self.dao.active_with_slots(bot_id, uid, scope, group_id, new_id, 300)
        superseded: List[int] = []
        for row in rows:
            old_slots = _slot_facts(str(row["content"] or ""))
            shared = set(old_slots) & set(new_slots)
            if not shared:
                continue
            if not any(old_slots[key] != new_slots[key] for key in shared):
                continue
            old_tokens = _recall_tokens(str(row["content"] or ""))
            # 至少共享一个有信息量的二元组（"主人""搬家"…），避免把无关话题误判成冲突
            shared_tokens = {t for t in (set(new_tokens) & set(old_tokens))
                             if len(t) == 2 and t not in STOP_BIGRAMS}
            if len(shared_tokens) < 1:
                continue
            superseded.append(int(row["id"]))
        self.dao.supersede(new_id, superseded)
        return superseded

    def mark_superseded(self, memory_id: int, by_id: int = 0) -> bool:
        """手动把一条记忆标成过期（面板/人工纠正用）。"""
        return self.dao.mark_superseded(memory_id, by_id)

    def forget(self, bot_id: str, keyword: str) -> int:
        """管理员 /遗忘 <关键词>：物理删除包含关键词的所有记忆，返回条数。"""
        body = _clean(keyword, 50)
        if not body:
            return 0
        count = self.dao.delete_by_keyword(str(bot_id), body)
        logger.info("[MEMORY] /遗忘「%s」：删除 %d 条", body, count)
        return count

    # ---------- 读/删 ----------
    def list_memories(self, bot_id: str, uid: str = "", limit: int = 100,
                      pid: int = 0) -> List[Dict[str, Any]]:
        try:
            if not pid and uid and self.persons is not None:
                pid = self.persons.pid_for_uid(bot_id, uid)
            if uid and self.persons is not None:
                uid = self.persons.canonical_uid_for_uid(bot_id, uid) or uid
        except Exception as exc:
            logger.debug("[amo_data] optional step failed: %r", exc)
        return self.dao.list_by_person(bot_id, uid, pid, limit)

    def delete_memory(self, bot_id: str, memory_id: int) -> bool:
        return self.dao.delete(bot_id, memory_id)

    def bot_of_memory(self, memory_id: int) -> str:
        """面板只传记忆 id 时反查它属于哪个 bot（多 bot 库不会删漏）。"""
        return self.dao.bot_of(memory_id)

    # ---------- 夜间提炼（守护插件 03:00 调） ----------
    def nightly_candidates(self, hours: int = 24, limit: int = 300) -> List[Dict[str, Any]]:
        """近 N 小时写入的流水记忆，交给守护插件提炼成长期事实。"""
        return self.dao.recent_active(hours=hours, limit=limit)

    def nightly_archive(self, ids: List[int], bot_id: str = "", uid: str = "") -> int:
        """提炼成功后把原流水标记 archived（不物理删除）；返回实际归档条数。

        调用方传的 ids 一律不可信：带 bot_id/uid 时 UPDATE 里再加这两个条件，
        不在名下的 id 直接跳过（只打一行日志），绝不归档别人的记忆。
        """
        clean: List[int] = []
        for value in (ids or []):
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in clean:
                clean.append(number)
        if not clean:
            return 0
        bounded = clean[:500]
        archived = int(self.dao.mark_archived(bounded, bot_id, uid) or 0)
        skipped = len(bounded) - archived
        if skipped > 0:
            logger.warning(
                "[amo_data] 夜间归档：%d 条记忆不属于 bot=%s uid=%s，已跳过",
                skipped, bot_id or "*", uid or "*",
            )
        return archived

    # ---------- 来源平台回填 ----------
    def backfill_memory_platform(self, bot_id: str = "", window_sec: int = 600,
                                 limit: int = 20000, apply: bool = True) -> Dict[str, Any]:
        """给 platform 为空的老记忆补「来源平台」，只补能确定的那部分。

        两条依据，任一成立才补：
        1) 同一句话在 messages 里出现过 → 直接用那条消息的平台；
        2) 写这条记忆的前后 window_sec 秒里，这个人只有某一个平台在说话 → 用那个平台。
        两个平台都在说、或前后根本没有消息 → 保持空（未知来源），绝不猜。
        """
        clause, params = ("", []) if not bot_id else (" AND bot_id=?", [str(bot_id)])
        result: Dict[str, Any] = {"scanned": 0, "filled": 0, "ambiguous": 0,
                                  "unknown": 0, "applied": bool(apply)}
        rows = self.dao.platform_blank_rows(clause, params, limit)
        result["scanned"] = len(rows)
        updates = []
        # 摘要类记忆本来就跨平台，不能按"前后谁在说话"推平台
        aggregate = {"conversation_summary", "daily_digest", "milestone",
                     "relationship_phase_summary", "imported_memory"}
        link_cache = {}
        for row in rows:
            bot = str(row["bot_id"])
            pid = int(row["pid"] or 0)
            uid = str(row["uid"] or "")
            body = str(row["content"] or "")
            kind = str(row["kind"] or "")
            plat = ""
            hit = self.dao.message_platform_by_content(bot, body)
            if hit:
                plat = hit
            # 2) 摘要类正文里会写参与者的 uid：只命中一个平台就用它
            if not plat:
                ckey = (bot, pid)
                if ckey not in link_cache:
                    link_cache[ckey] = self.dao.links_for(bot, pid, uid)
                plats = {str(l["platform"]).strip() for l in link_cache[ckey]
                         if str(l["uid"] or "") and str(l["uid"]) in body
                         and str(l["platform"] or "").strip()}
                if len(plats) == 1:
                    plat = plats.pop()
            # 3) 前后 window_sec 只有一个平台在说话：单轮记忆才用，摘要类跳过
            if not plat and kind not in aggregate:
                near = self.dao.near_platforms(bot, pid, uid, int(row["created_at"] or 0), window_sec)
                plats = {str(r["platform"]).strip() for r in near
                         if str(r["platform"] or "").strip()}
                if len(plats) == 1:
                    plat = plats.pop()
                elif len(plats) > 1:
                    result["ambiguous"] += 1
                    continue
            if not plat:
                result["unknown"] += 1
                continue
            updates.append((plat, int(row["id"])))
        result["filled"] = len(updates)
        if apply and updates:
            self.dao.set_platform(updates)
        return result


def _try_merge(engine, bot_id, uid, scope, group_id, text, importance, now,
               subject, fact, static, keep_existing_content, idem_key: str = "",
               tags: str = "") -> Optional[Dict[str, Any]]:
    """同一件事重复写入：抬重要度/合并/刷新，不新增行；返回结果或 None。"""
    if idem_key:
        row = engine.dao.find_active_by_idem(bot_id, uid, scope, group_id, idem_key)
        if row:
            try:
                boost = min(1.0, max(float(row["importance"] or 0), float(importance)) + 0.03)
            except (TypeError, ValueError):
                boost = importance
            if fact:
                old_text = str(row["content"] or "")
                merged_text = fact_dedupe.merge_fact(old_text, text)
                if merged_text and merged_text != old_text:
                    engine.dao.merge_content(int(row["id"]), merged_text, boost, now, subject,
                                             False, tags)
                    engine._enqueue_embedding(int(row["id"]), merged_text)
                else:
                    engine.dao.touch(int(row["id"]), boost, now, subject, tags)
            elif keep_existing_content:
                engine.dao.touch(int(row["id"]), boost, now, subject, tags)
            else:
                # 同一个 idem 的非静态事实（事件/流水）：最新正文覆盖旧正文，
                # 不再只 touch 一下把新内容静默丢掉（Phase 12 修）。
                old_text = str(row["content"] or "")
                if old_text != text:
                    engine.dao.merge_content(int(row["id"]), text, boost, now, subject,
                                             False, tags)
                    engine._enqueue_embedding(int(row["id"]), text)
                else:
                    engine.dao.touch(int(row["id"]), boost, now, subject, tags)
            return {"ok": True, "memory_id": int(row["id"]), "duplicate": True}
        # 同一 idem_key 的旧行被归档/取代/合并后仍然占着唯一索引：再写同一件事必须
        # 复活/并回它，否则 INSERT 撞 UNIQUE 约束、整条记忆被静默丢掉。
        stale = engine.dao.find_by_idem(bot_id, uid, scope, group_id, idem_key)
        if stale is not None:
            base_id = int(stale["superseded_by"] or 0)
            status = str(stale["status"] or "active")
            if (status == "merged" and base_id > 0
                    and engine.dao.touch(base_id, importance, now, subject, tags)):
                return {"ok": True, "memory_id": base_id, "duplicate": True}
            engine.dao.revive(int(stale["id"]), importance, now, subject, tags)
            return {"ok": True, "memory_id": int(stale["id"]), "duplicate": True}
    if engine.setting("auto_dedupe", True) and static:
        cand = engine._near_duplicate(bot_id, uid, scope, group_id, text)
        if cand is not None:
            engine.dao.merge_content(int(cand["id"]), text, importance, now, subject,
                                     bool(keep_existing_content), tags)
            if not keep_existing_content:
                engine._enqueue_embedding(int(cand["id"]), text)
            return {"ok": True, "memory_id": int(cand["id"]), "duplicate": True}
    return None

