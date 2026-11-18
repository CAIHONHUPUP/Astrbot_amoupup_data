# -*- coding: utf-8 -*-
"""utils/config.py —— 插件配置 -> 强类型设置；检索参数沿用 3.0 的默认值。"""

from __future__ import annotations

from typing import Any, Dict

try:
    from ..utils.text_process import DEFAULT_SETTINGS, RECALL_POOL
except ImportError:  # 顶层导入（测试/脚本）
    from utils.text_process import DEFAULT_SETTINGS, RECALL_POOL

PLUGIN_NAME = "Astrbot_amoupup_data"
PLUGIN_VERSION = "3.4.0"  # 与 main.py / metadata.yaml 同步；/status 报的就是这个值


class PluginSettings:
    """把面板配置翻译成引擎要的扁平字典（缺的用 3.0 默认值）。"""

    def __init__(self, config=None):
        # 不拷贝：面板/测试会直接改 plugin.config，弱类型读取要能看到最新值
        self.config: Dict[str, Any] = config if isinstance(config, dict) else dict(config or {})
        self._values: Dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._values.update(self._store_settings())

    def reload(self, config=None) -> None:
        """配置热更新：原地刷新 _values（引擎持有的是同一个 settings 对象）。"""
        if isinstance(config, dict):
            self.config = config
        self._values = dict(DEFAULT_SETTINGS)
        self._values.update(self._store_settings())

    # ---------- 读取 ----------
    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
        return bool(value)

    def _store_settings(self) -> Dict[str, Any]:
        return {
            "pool_size": self._cfg_int("recall_pool_size", RECALL_POOL),
            "chain_trace": self._cfg_bool("chain_trace", True),
            "config_watch": self._cfg_bool("config_watch", True),
            "recall_fts_enabled": self._cfg_bool("recall_fts_enabled", False),
            "recall_recent_pool": self._cfg_int("recall_recent_pool", 600),
            "recall_vector_candidates": self._cfg_int("recall_vector_candidates", 50),
            "recall_token_or": self._cfg_bool("recall_token_or", True),
            "recall_vec_gate": str(self.config.get("recall_vec_gate") or "rank"),
            "recall_vec_gate_topk": self._cfg_int("recall_vec_gate_topk", 2),
            "recall_vec_gate_floor": self._cfg_float("recall_vec_gate_floor", 0.80),
            "recall_vec_gate_window": self._cfg_float("recall_vec_gate_window", 0.08),
            "recall_graph_route": self._cfg_bool("recall_graph_route", True),
            "recall_graph_candidates": self._cfg_int("recall_graph_candidates", 10),
            "recall_multi_query": self._cfg_bool("recall_multi_query", True),
            "recall_multi_query_variants": self._cfg_int("recall_multi_query_variants", 3),
            "recall_entity_tags": self._cfg_bool("recall_entity_tags", True),
            "recall_rerank_enabled": self._cfg_bool("recall_rerank_enabled", False),
            "recall_rerank_candidates": self._cfg_int("recall_rerank_candidates", 20),
            "recall_rerank_timeout": self._cfg_float("recall_rerank_timeout", 8.0),
            "recall_entity_graph": self._cfg_bool("recall_entity_graph", True),
            "recall_entity_graph_memories": self._cfg_int("recall_entity_graph_memories", 10),
            "recall_revive_dormant": self._cfg_bool("recall_revive_dormant", True),
            "lifecycle_enabled": self._cfg_bool("lifecycle_enabled", True),
            "lifecycle_noise_days": self._cfg_int("lifecycle_noise_days", 30),
            "lifecycle_summary_days": self._cfg_int("lifecycle_summary_days", 180),
            "lifecycle_expire_days": self._cfg_int("lifecycle_expire_days", 365),
            "lifecycle_purge_expired_days": self._cfg_int("lifecycle_purge_expired_days", 0),
            "lifecycle_consolidate_days": self._cfg_int("lifecycle_consolidate_days", 7),
            "lifecycle_consolidate_min": self._cfg_int("lifecycle_consolidate_min", 3),
            "semantic_consolidation_enabled": self._cfg_bool("semantic_consolidation_enabled", True),
            "semantic_consolidation_max_groups": self._cfg_int("semantic_consolidation_max_groups", 3),
            "semantic_consolidation_timeout": self._cfg_float("semantic_consolidation_timeout", 20.0),
            "min_matched_terms": self._cfg_int("recall_min_matched_terms", 1),
            "forget_half_life_days": self._cfg_float("recall_half_life_days", 45.0),
            "importance_decay": self._cfg_float("recall_importance_decay", 0.10),
            "auto_dedupe": self._cfg_bool("auto_dedupe", True),
            "near_duplicate_ratio": self._cfg_float("auto_dedupe_ratio", 0.92),
            "embedding_enabled": self._cfg_bool("embedding_enabled", False),
            "embedding_base": str(self.config.get("embedding_base") or "http://127.0.0.1:11434"),
            "embedding_model": str(self.config.get("embedding_model") or "bge-m3"),
            "embedding_timeout": self._cfg_float("embedding_timeout", 3.0),
            "vec_weight": self._cfg_float("recall_vec_weight", 0.45),
            "vec_min_cos": self._cfg_float("recall_vec_min_cos", 0.45),
            "vec_min_margin": self._cfg_float("recall_vec_min_margin", 0.08),
            "embedding_backfill": self._cfg_bool("embedding_backfill", True),
            "embedding_backfill_limit": self._cfg_int("embedding_backfill_limit", 200),
        }

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._values:
            return self._values[key]
        return self.config.get(key, default)

    # ---------- 保留策略 ----------
    @property
    def chat_days(self) -> int:
        return max(1, self._cfg_int("chat_days", 14))

    @property
    def compress_after(self) -> int:
        return max(1, self._cfg_int("chat_compress_after", 3))

    @property
    def log_days(self) -> int:
        return max(1, self._cfg_int("log_days", 7))
