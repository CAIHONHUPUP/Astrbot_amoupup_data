# -*- coding: utf-8 -*-
"""services/memory_engine.py —— 写记忆：清洗、幂等去重、近重复合并、取代、向量、来源回填。

判定都在这一层；SQL 在 models/dao.py。
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional

try:
    from ..models.dao import MemoryDao
    from ..utils.text_process import (
        STOP_BIGRAMS, _clean, _jaccard, _now, _norm_key, _recall_tokens, _slot_facts, logger,
    )
except ImportError:  # 顶层导入（测试/脚本）
    from models.dao import MemoryDao
    from utils.text_process import (
        STOP_BIGRAMS, _clean, _jaccard, _now, _norm_key, _recall_tokens, _slot_facts, logger,
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
        self._emb_warned = False
        if self.setting("embedding_enabled"):
            self._ensure_emb_worker()
            if self.setting("embedding_backfill"):
                try:
                    self.backfill_embeddings(limit=int(self.setting("embedding_backfill_limit", 200)))
                except Exception:
                    pass

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
                except Exception:
                    pass
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
        except Exception:
            pass

    def _load_vecs(self, ids: List[int]) -> Dict[int, List[float]]:
        return self.dao.load_vecs(ids)

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
        except Exception:
            pass

    # ---------- 写 ----------
    def remember(self, bot_id: str, uid: str, content: str, kind: str = "fact",
                 importance: float = 0.5, scope: str = "private", group_id: str = "",
                 subject: str = "user", platform: str = "", idem_key: str = "",
                 created_at: Optional[int] = None, last_hit_at: Optional[int] = None,
                 hit_count: int = 0, keep_existing_content: bool = False) -> Dict[str, Any]:
        text = _clean(content)
        if not text:
            return {"ok": False, "reason": "empty"}
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
        scope = "group" if str(scope) == "group" else "private"
        group_id = str(group_id or "") if scope == "group" else ""
        try:
            importance = max(0.0, min(1.0, float(importance)))
        except (TypeError, ValueError):
            importance = 0.5
        now = _now()
        try:
            created_at = int(created_at) if created_at else now
        except (TypeError, ValueError):
            created_at = now
        try:
            last_hit_at = int(last_hit_at) if last_hit_at else created_at
        except (TypeError, ValueError):
            last_hit_at = created_at
        try:
            hit_count = max(0, int(hit_count or 0))
        except (TypeError, ValueError):
            hit_count = 0
        idem_key = _clean(idem_key, 120)
        subject = "bot" if str(subject or "").strip().lower() == "bot" else "user"
        if not idem_key and self.setting("auto_dedupe", True):
            idem_key = _norm_key(text)

        if idem_key:
            row = self.dao.find_active_by_idem(bot_id, uid, scope, group_id, idem_key)
            if row:
                # 同一件事又说了一遍：抬一点重要度、刷新时间，不新增行
                self.dao.touch(int(row["id"]), importance, now, subject)
                return {"ok": True, "memory_id": int(row["id"]), "duplicate": True}

        # 近重复（换了标点/语序）也合并：二元组 Jaccard 超过阈值
        if self.setting("auto_dedupe", True):
            cand = self._near_duplicate(bot_id, uid, scope, group_id, text)
            if cand is not None:
                self.dao.merge_content(int(cand["id"]), text, importance, now, subject,
                                       bool(keep_existing_content))
                if not keep_existing_content:
                    self._enqueue_embedding(int(cand["id"]), text)
                return {"ok": True, "memory_id": int(cand["id"]), "duplicate": True}

        memory_id = self.dao.insert(
            bot_id, uid, pid, platform, scope, group_id, _clean(kind, 32) or "fact", subject,
            text, importance, hit_count, created_at, last_hit_at, idem_key,
        )
        if self.setting("identity_debug", True):
            logger.info(
                "[MEMORY_DEBUG] person_id=%s source_platform=%s hit=%s reason=write:%s",
                pid, platform or "unknown", 0, str(kind or "fact"),
            )
        self._enqueue_embedding(memory_id, text)
        try:
            superseded = self._detect_supersede(bot_id, uid, scope, group_id, memory_id, text)
        except Exception:
            superseded = []
        return {"ok": True, "memory_id": memory_id, "duplicate": False,
                "created_at": created_at, "superseded": superseded}

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

    # ---------- 读/删 ----------
    def list_memories(self, bot_id: str, uid: str = "", limit: int = 100,
                      pid: int = 0) -> List[Dict[str, Any]]:
        try:
            if not pid and uid and self.persons is not None:
                pid = self.persons.pid_for_uid(bot_id, uid)
            if uid and self.persons is not None:
                uid = self.persons.canonical_uid_for_uid(bot_id, uid) or uid
        except Exception:
            pass
        return self.dao.list_by_person(bot_id, uid, pid, limit)

    def delete_memory(self, bot_id: str, memory_id: int) -> bool:
        return self.dao.delete(bot_id, memory_id)

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
