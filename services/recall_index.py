# -*- coding: utf-8 -*-
"""services/recall_index.py —— 全库向量候选层（kNN）与多路 RRF 融合。

为什么要有这一层：memory_vec 里的向量原来只给「已经进候选池」的行重新打分，
候选池只有「整句子串 LIKE」和「最近活跃 600 条」两路。库一大，词面不重合的
记忆就永远进不了候选；而且 last_hit_at 会被召回刷新，被召回过的记忆更容易
再进池（富者愈富），从没被召回的就一直沉底。

这一层补上第三路候选，并负责融合三路名次：
    * VectorIndex.build/ensure：一次性把 memory_vec 读进内存矩阵（numpy 可选）；
    * search(q_vec, k)：全库 kNN，返回 [(id, 余弦)]；
    * cosines(q_vec, ids)：给候选池补算余弦（numpy 加速，纯 Python 兜底）；
    * rrf(routes, k=60)：标准 Reciprocal Rank Fusion，纯函数、可单测。

线程与生命周期：由 RetrievalEngine 在 asyncio.to_thread 的 worker 里调用；
读向量走 DB 锁，构建完只读；向量条数/最大 id/更新时间变化时自动重建。
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: RRF 经典平滑常数（Cormack et al. 2009）；越大越平权
RRF_K = 60


def rrf(routes: Sequence[Sequence[int]], k: int = RRF_K) -> List[int]:
    """把多路「已按名次排好」的 id 列表融合成一个顺序（分高在前，同分按 id）。"""
    bonus: Dict[int, float] = {}
    for route in routes:
        for rank, doc_id in enumerate(route, start=1):
            try:
                did = int(doc_id)
            except (TypeError, ValueError):
                continue
            bonus[did] = bonus.get(did, 0.0) + 1.0 / (k + rank)
    return [did for did, _ in sorted(bonus.items(), key=lambda kv: (-kv[1], kv[0]))]


def _unit(vec: Sequence[float]) -> List[float]:
    total = 0.0
    for raw in vec:
        try:
            total += float(raw) * float(raw)
        except (TypeError, ValueError):
            return []
    length = math.sqrt(total)
    if length <= 0:
        return []
    return [float(raw) / length for raw in vec]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    total = 0.0
    for x, y in zip(a, b):
        total += float(x) * float(y)
    return total


class VectorIndex:
    """memory_vec 的内存快照 + 余弦检索；numpy 缺失时退回纯 Python。"""

    def __init__(self, loader: Callable[[], Tuple[Tuple[int, int, int], Dict[int, List[float]]]]):
        self._loader = loader
        self._state: Optional[Tuple[int, int, int]] = None
        self._built = False
        self._ids: List[int] = []
        self._vecs: Dict[int, List[float]] = {}
        self._matrix: Any = None  # numpy 归一化矩阵（行=id）；没装 numpy 时是 None
        self._row_of: Dict[int, int] = {}

    def invalidate(self) -> None:
        self._built = False

    def ensure(self) -> None:
        state, raw = self._loader()
        if self._built and state == self._state:
            return
        ids: List[int] = []
        units: List[List[float]] = []
        for mid, vec in raw.items():
            unit = _unit(vec)
            if not unit:
                continue
            ids.append(int(mid))
            units.append(unit)
        self._ids = ids
        self._vecs = dict(zip(ids, units))
        self._row_of = {mid: i for i, mid in enumerate(ids)}
        self._state = state
        self._built = True
        self._matrix = None
        if units:
            try:
                import numpy as np  # 可选依赖：没装也不影响召回
                self._matrix = np.asarray(units, dtype="float32")
            except Exception:
                self._matrix = None

    def __len__(self) -> int:
        self.ensure()
        return len(self._ids)

    def search(self, q_vec: Sequence[float], k: int = 50) -> List[Tuple[int, float]]:
        """全库 kNN：返回余弦最高的 k 个 (id, 余弦)。"""
        self.ensure()
        wanted = max(1, int(k or 1))
        unit = _unit(q_vec)
        if not unit or not self._ids:
            return []
        dim = len(unit)
        if self._matrix is not None:
            import numpy as np
            if int(self._matrix.shape[1]) != dim:
                return []
            scores = self._matrix @ np.asarray(unit, dtype="float32")
            order = np.argsort(-scores)[: wanted]
            return [(self._ids[int(i)], float(scores[int(i)])) for i in order]
        pairs = [(mid, _dot(unit, vec)) for mid, vec in self._vecs.items()
                 if len(vec) == dim]
        pairs.sort(key=lambda kv: (-kv[1], kv[0]))
        return pairs[:wanted]

    def cosines(self, q_vec: Sequence[float], ids: Sequence[int]) -> Dict[int, float]:
        """只给指定 id 算余弦（候选池补分用）。"""
        self.ensure()
        unit = _unit(q_vec)
        if not unit:
            return {}
        out: Dict[int, float] = {}
        if self._matrix is not None:
            import numpy as np
            if int(self._matrix.shape[1]) != len(unit):
                return {}
            q = np.asarray(unit, dtype="float32")
            for mid in ids:
                row = self._row_of.get(int(mid))
                if row is not None:
                    out[int(mid)] = float(self._matrix[row] @ q)
            return out
        for mid in ids:
            vec = self._vecs.get(int(mid))
            if vec and len(vec) == len(unit):
                out[int(mid)] = _dot(unit, vec)
        return out
