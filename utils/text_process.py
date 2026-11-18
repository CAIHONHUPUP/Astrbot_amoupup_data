# -*- coding: utf-8 -*-
"""能记的常量与纯函数：平台归一、检索打分（BM25/遗忘曲线）、时间窗等。

从 store.py 搬出来，store_* 各模块都从这里取，避免循环 import。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import hashlib
import math
import re
import struct
import time
from typing import Any, Dict, Iterable, List

if TYPE_CHECKING:  # 只给类型检查看：包内相对导入
    from . import clock as _clock
else:  # 运行时：包模式优先，按文件直接加载时退回绝对导入
    try:
        from . import clock as _clock
    except Exception:
        from utils import clock as _clock

# 时间解析必须两种导入都试：AstrBot 以 data.plugins.Astrbot_amoupup_data 包方式加载时，
# 函数里的顶层 from time_query import ...（根目录兼容壳已删）会 ModuleNotFoundError、被 except 吞掉，
# span 恒为 None，于是「问昨天 → 拉当天日总结」的整段逻辑从来没执行过。
if TYPE_CHECKING:  # 只给类型检查看：包内相对导入
    from .time_query import parse_time_span as _parse_time_span
else:  # 运行时：包模式优先，按文件直接加载时退回绝对导入
    try:
        from .time_query import parse_time_span as _parse_time_span
    except Exception:
        try:
            from utils.time_query import parse_time_span as _parse_time_span
        except Exception:
            _parse_time_span = None

if TYPE_CHECKING:  # 只给类型检查看：包内相对导入
    from astrbot.api import logger
else:  # 运行时：包模式优先，按文件直接加载时退回绝对导入
    try:
        from astrbot.api import logger
    except Exception:  # 单测/纯逻辑场景允许脱离 AstrBot 运行
        # 规范：logger 只能从 astrbot.api 导入，禁止回退内置 logging。
        # 脱离 AstrBot 时用空实现，既不写日志也不改全局 logging 配置。
        class _NullLogger:
            def __getattr__(self, name):
                def _noop(*args, **kwargs):
                    return None

                return _noop

        logger = _NullLogger()


def _now() -> int:
    return int(time.time())


def _clean(text: Any, limit: int = 2000) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


def _norm_tags(tags: Any, limit: int = 200) -> str:
    """标签归一化：折叠空白、去首尾、限长；None/空 -> ''（空值不覆盖旧标签）。"""
    return _clean(tags, limit)


def _bot_filter(bot_id: str, column: str = "bot_id"):
    """bot_id 为空表示不过滤（面板默认看全部 bot）。"""
    value = str(bot_id or "").strip()
    if not value:
        return "", []
    return " AND %s=?" % column, [value]


# 平台前缀 -> 给人看的名字。Amo 传进来的平台名可能是 aiocqhttp / lark / qq_official …
PLATFORM_LABELS = {
    "aiocqhttp": "QQ",
    "qq": "QQ",
    "qq_official": "QQ",
    "qqofficial": "QQ",
    "onebot": "QQ",
    "napcat": "QQ",
    "lark": "飞书",
    "feishu": "飞书",
    "wecom": "企业微信",
    "wework": "企业微信",
    "dingtalk": "钉钉",
    "telegram": "Telegram",
    "discord": "Discord",
    "slack": "Slack",
    "kook": "KOOK",
    "satori": "Satori",
    "webchat": "网页",
    "websocket": "网页",
    "wechat": "微信",
    "wx": "微信",
}


def platform_label(platform: str) -> str:
    key = str(platform or "").strip().lower()
    if not key:
        return "未知"
    return PLATFORM_LABELS.get(key, str(platform).strip())


PLATFORM_ALIASES = {"qq": "aiocqhttp", "onebot": "aiocqhttp", "onebotv11": "aiocqhttp",
                    "飞书": "lark", "feishu": "lark"}
KNOWN_PLATFORMS = {"aiocqhttp", "lark", "webchat", "telegram", "discord", "slack", "wecom"}


def normalize_platform(platform: str = "", umo: str = "") -> str:
    """平台名归一。

    坑：QQ 的 umo 前缀是**机器人名字**（``大肥鱼:FriendMessage:302578963``），不是平台名，
    以前 ``platform_of_umo`` 直接取第一段，于是她的回复被写成 platform='大肥鱼'，
    跨平台查询按 platform='aiocqhttp' 就查不到那半边（QQ 聊天在库里被劈成两份）。
    """
    value = str(platform or "").strip()
    head = str(umo or "").strip().split(":", 1)[0].strip()
    for candidate in (value, head):
        key = candidate.lower()
        if key in PLATFORM_ALIASES:
            return PLATFORM_ALIASES[key]
        if key in KNOWN_PLATFORMS:
            return key
    # 都不是已知平台：QQ 那种「用 bot 名字当 umo 前缀」的，按 Friend/GroupMessage 认
    if head and (":FriendMessage:" in str(umo) or ":GroupMessage:" in str(umo)):
        return "aiocqhttp"
    return value.lower() or head.lower()


def platform_of_umo(umo: str) -> str:
    """umo 形如 ``aiocqhttp:GroupMessage:123456``：能认的取平台，认不出按 QQ 兜底。"""
    text = str(umo or "").strip()
    if not text:
        return ""
    return normalize_platform("", text)


def _name_key(text: Any) -> str:
    """认人用的名字归一化：去掉空白和常见装饰符号，兼容大小写。"""
    value = re.sub(r"[\s\u3000·•、,，。.!！?？~～\-—_]+", "", str(text or ""))
    return value.strip().lower()[:32]



# ---------------------------------------------------------------------------
# 检索打分（BM25 + importance + 遗忘曲线）
# ---------------------------------------------------------------------------

RECALL_POOL = 400              # 候选池上限：先粗筛这么多条，再精排
RECALL_HALF_LIFE_DAYS = 45.0   # 遗忘半衰期（天）：45 天新鲜度掉一半
RECALL_W_REL = 0.60            # 相关性权重
RECALL_W_IMPORTANCE = 0.30     # 重要度权重
RECALL_W_FRESH = 0.10          # 新鲜度权重
RECALL_IMPORTANCE_DECAY = 0.10 # importance 自身也随时间软化（0=不衰减）
RECALL_MIN_REL = 0.05          # 有 query 时，相关性低于此值直接丢弃


# ---------------------------------------------------------------------------
# 本地增强（2.3.x patch）：相关性闸 / 自适应候选池 / 自动去重 / 可选向量
# ---------------------------------------------------------------------------

# 常见虚词：只参与 BM25（靠 IDF 自动降权），不单独作为"命中证据"
STOP_CHARS = set(
    "我你他她它的了吗呢啊吧呀哦嗯是在和就也都这那有没不很太会要个些么什怎样多少"
    "还记说给把被让对从到又再才只更最得着过们嘛哪谁几"
)

# 只表示时间/泛指、但没有信息量的二元组：不能单独当作"她想起来了"的证据
STOP_BIGRAMS = {
    "今天", "明天", "现在", "刚才", "最近", "时候", "事情", "东西",
    "这个", "那个", "什么", "怎么", "怎样", "多少", "是不", "不是",
}

DEFAULT_SETTINGS: Dict[str, Any] = {
    "pool_size": RECALL_POOL,          # 粗筛池；会按库大小自适应放大
    "min_matched_terms": 1,            # 至少命中几个"有信息量"的词，0=关掉这道闸
    "forget_half_life_days": RECALL_HALF_LIFE_DAYS,
    "importance_decay": RECALL_IMPORTANCE_DECAY,
    "auto_dedupe": True,               # 调用方没给 idem_key 时按内容指纹去重
    "near_duplicate_ratio": 0.92,      # 二元组 Jaccard 超过它就当同一件事
    "embedding_enabled": False,        # 库层默认关；插件默认开（见 _conf_schema）
    "embedding_base": "http://127.0.0.1:11434",
    "embedding_model": "bge-m3",
    "embedding_timeout": 3.0,
    "vec_weight": 0.45,                # 语义分在相关性里的占比
    "vec_min_cos": 0.82,               # 纯语义命中的最低余弦（bge-m3 中文普遍偏平）
    "vec_min_margin": 0.08,            # top 与第二名的余弦差；太小说明"谁都不像"
    "diversify_ratio": 0.60,        # 注入时同一件事只留一条（0=关闭多样化）；0.6 见 work/REPORT.md 实测
    "embedding_backfill": True,        # 启动时给旧记忆补向量
    "embedding_backfill_limit": 5000,  # 启动时最多排队补多少条（后台线程慢慢算）
    "recall_fts_enabled": False,       # 可选：FTS5 trigram 子串粗筛（默认关，见 CHANGELOG 实测）
    "recall_recent_pool": 600,         # 「最近活跃」这一层的上限，库大了不至于把几条热门记忆全捞进来
    "recall_vector_candidates": 50,    # 全库向量 kNN 候选条数（0=关）；补「词面不重合」的记忆
    "recall_token_or": True,           # 整句 LIKE 之外再拆词 OR 粗筛（跨词序也能进候选）
    "recall_vec_gate": "rank",         # 纯语义命中闸：rank=只看和 top 差多少；strict=旧绝对门槛
    "recall_vec_gate_topk": 2,         # rank 模式：最多放行 top 几名（A/B：2 比 3 少噪声）
    "recall_vec_gate_floor": 0.80,     # rank 模式：余弦绝对底线（A/B：0.80 不牺牲弃答率）
    "recall_vec_gate_window": 0.08,    # rank 模式：与 top 的最大差距
    "recall_graph_route": True,        # 实体/关系路：名字、别名、关系词命中人物时按 pid 取记忆
    "recall_graph_candidates": 10,     # 这一路最多带几条候选
    "recall_multi_query": True,        # 剥壳/拆词变体各粗筛一次再合并（多查询）
    "recall_multi_query_variants": 3,  # 最多几个变体（含原句）
    "recall_entity_tags": True,        # 写入时给正文里出现的人物名字挂 entity:<名字> 标签
    "recall_rerank_enabled": False,    # 二阶段 LLM 重排（默认关；开了多一次小模型调用）
    "recall_rerank_candidates": 20,    # 重排最多看几条
    "recall_rerank_timeout": 8.0,      # 重排超时（秒），超时/失败原样返回
    "recall_entity_graph": True,       # 通用实体图谱：写时抽实体连边，查时按实体+一跳共现取记忆
    "recall_entity_graph_memories": 10,  # 实体图谱最多带几条候选
    "recall_revive_dormant": True,     # 强关键词命中休眠记忆时复活它
    "lifecycle_enabled": True,         # 每天按 kind TTL 休眠/过期
    "lifecycle_noise_days": 30,        # 流水账休眠天数（0=用内置表）
    "lifecycle_summary_days": 180,     # 摘要休眠天数
    "lifecycle_expire_days": 365,      # 流水账过期天数
    "lifecycle_purge_expired_days": 0, # 过期后再过 N 天物理删除（0=只标记不删）
    "lifecycle_consolidate_days": 7,   # 巩固：几天前的流水开始合并
    "lifecycle_consolidate_min": 3,    # 巩固：同天同会话至少几条才合
    "semantic_consolidation_enabled": True,   # 跨天同主题语义巩固（每天最多几组小模型调用）
    "semantic_consolidation_max_groups": 3,
    "semantic_consolidation_timeout": 20.0,
    "summary_merge_max_chars": 800,    # 同天摘要合并上限；超过就拆条，别造 4000 字怪物
    "meta_query_fallback": True,       # 「你还记得我吗」这类没有实词的查询按重要度兜底
    "meta_query_limit": 3,             # 兜底最多给几条
    "identity_debug": True,            # [IDENTITY_DEBUG]/[MEMORY_DEBUG] 日志开关
    "chain_trace": True,               # 召回/写入每一步打 [chain] trace（排障用）
    "config_watch": True,              # 面板保存配置后热更新，不用重启插件
}


def _norm_key(text: Any) -> str:
    """内容指纹：去掉空白和标点后取 sha1 前 16 位。"""
    raw = re.sub(
        r"[\s\u3000·•、,，。.!！?？~～\-—_\"'“”‘’（）()\[\]【】:：;；]+",
        "", str(text or ""),
    ).lower()
    if not raw:
        return ""
    return "auto:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _query_chars(text: Any) -> List[str]:
    """查询里的实词单字（去掉虚词），用于"短词模糊命中"的覆盖率判断。"""
    return [c for c in re.findall(r"[\u4e00-\u9fff]", str(text or "").lower())
            if c not in STOP_CHARS]


#: 纯连接词/代词：和它们拼成的二元组不是「词」（「我想」「的新」「听吗」「晚谁」），
#: 不能当命中证据；而「迟到」「记住」「个月」里的虚词本来就是构词的一部分，必须保留。
GLUE_CHARS = set("的了着过吗呢吧啊呀哦嗯嘛么什怎谁哪几我你他她它咱您")


def _informative_terms(text: Any) -> List[str]:
    """命中证据：保邻接的二元组 / 英文词；太短的查询退回单字。

    这是为了修掉"什么都能推一条"：只靠 BM25 相对分排序时，
    库里哪怕没有相关内容，最像的那条也会被归一化成满分。

    2026-11-17 真实回放评估修正：原实现先删虚词再配对，把「迟到」拆成
    「迟|到」再配成「迟罚」，「记住」配成「住两」，精确命中被关键词闸整条挡掉
    （66 条真实查询里 2 条空召回：迟到罚款 / 记住大米）。现在按原始字序列配对
    （保邻接），只丢弃两种二元组：
      * 两个字都是虚词的（了吗 / 多少 …，靠 STOP_CHARS）；
      * 含纯连接词/代词的（我想 / 的新 / 听吗 …，靠 GLUE_CHARS）。
    """
    s = str(text or "").lower()
    words = [w for w in re.findall(r"[a-z0-9_]{2,}", s) if w not in STOP_CHARS]
    chars = re.findall(r"[\u4e00-\u9fff]", s)
    if not chars:
        return words
    if len(chars) == 1:
        return words + chars
    pairs: List[str] = []
    for a, b in zip(chars, chars[1:]):
        if a in STOP_CHARS and b in STOP_CHARS:
            continue
        if a in GLUE_CHARS or b in GLUE_CHARS:
            continue
        pairs.append(a + b)
    return words + [pair for pair in pairs if pair not in STOP_BIGRAMS]


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


# 常见"会变的事实"槽位：同一个人身上出现不同值就说明旧的过期了
SLOT_PATTERNS = (
    ("home", re.compile(r"(?:住在|住到|搬到|搬家到|搬去)\s*([\u4e00-\u9fffA-Za-z0-9\u00b7\-]{2,14})")),
    ("work", re.compile(r"在\s*([\u4e00-\u9fffA-Za-z0-9\u00b7\-]{2,14})\s*(?:上班|工作|公司)")),
    ("birthday", re.compile(r"生日(?:是|在)?\s*(\d{1,2}\s*月\s*\d{1,2}\s*[日号])")),
    ("job", re.compile(r"(?:职业是|工作是|做的?是)\s*([\u4e00-\u9fffA-Za-z0-9]{2,10})")),
)


def _slot_facts(text: Any) -> Dict[str, str]:
    """抽出"住在哪/在哪上班/生日/职业"这类槽位值；抽不到就是空表。"""
    raw = str(text or "")
    out: Dict[str, str] = {}
    for key, pattern in SLOT_PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue
        value = re.sub(r"\s+", "", match.group(1) or "")
        # 去掉"住了/那边/一带/附近/了/的"这类尾巴，否则同一个地点会算出两个值
        value = re.sub(r"(住了|住|那边|这边|一带|附近|旁边|了|的)+$", "", value)
        # 「记住北京的咖啡」这类会被误当成住址：带虚词的不是地名，直接不要
        if not value or re.search(r"[的了着这那]", value):
            continue
        # 只保留地名/名词部分（去掉句末标点）
        value = value.strip("，。！？、;；")
        if value:
            out[key] = value
    return out


def _to_vec(blob: Any, dim: int) -> List[float]:
    """BLOB -> list[float]；只认 float32，长度不对就当无效。"""
    try:
        raw = bytes(blob or b"")
    except Exception:
        return []
    if not raw or dim <= 0 or len(raw) != dim * 4:
        return []
    try:
        return list(struct.unpack("<%df" % dim, raw))
    except Exception:
        return []


def _from_vec(vec: List[float]) -> bytes:
    return struct.pack("<%df" % len(vec), *[float(x) for x in vec])



def _recall_tokens(text: Any) -> List[str]:
    """中文按 单字 + 二元组 切，英文数字按词切。

    中文没有空格，FTS5 的 unicode61 会把整句当一个 token，
    「两个字」「上班」这种短词永远 MATCH 不到；这里用单字+二元组，
    保证「猫」「茶」「上班」这种 1~2 字输入也能参与打分。
    """
    s = str(text or "").lower()
    tokens: List[str] = re.findall(r"[a-z0-9_]{2,}", s)
    chars = re.findall(r"[\u4e00-\u9fff]", s)
    tokens.extend(chars)
    tokens.extend(["".join(pair) for pair in zip(chars, chars[1:])])
    return tokens


def _bm25_scores(q_tokens: List[str], docs: List[List[str]], k1: float = 1.4,
                 b: float = 0.75) -> List[float]:
    """标准 BM25（Okapi），返回每个 doc 的原始分。"""
    if not docs or not q_tokens:
        return [0.0 for _ in docs]
    n = len(docs)
    avgdl = sum(len(d) for d in docs) / float(n) or 1.0
    df: Dict[str, int] = {}
    for d in docs:
        for term in set(d):
            df[term] = df.get(term, 0) + 1
    out: List[float] = []
    for d in docs:
        dl = len(d) or 1
        score = 0.0
        tf: Dict[str, int] = {}
        for term in d:
            tf[term] = tf.get(term, 0) + 1
        for term in q_tokens:
            if term not in tf:
                continue
            freq = tf[term]
            idf = (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5)
            idf = math.log(1.0 + idf)
            score += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * dl / avgdl))
        out.append(score)
    return out


def _freshness(ts: Any, now: int, half_life_days: float) -> float:
    """遗忘曲线：0.5 ** (age/half_life)。未来的时间按 1 处理。"""
    try:
        stamp = float(ts or 0)
    except (TypeError, ValueError):
        stamp = 0.0
    if stamp <= 0:
        return 1.0
    age_days = max(0.0, (now - stamp) / 86400.0)
    hl = max(1.0, float(half_life_days or RECALL_HALF_LIFE_DAYS))
    return 0.5 ** (age_days / hl)



def _unique_labels(links) -> list:
    """同一个人可能同时有 qq / aiocqhttp 两条链接，面板只显示一次 QQ。"""
    out = []
    for item in links or []:
        label = str(item.get("label") or item.get("platform") or "")
        if label and label not in out:
            out.append(label)
    return out


__all__ = [
    "DEFAULT_SETTINGS",
    "KNOWN_PLATFORMS",
    "PLATFORM_ALIASES",
    "PLATFORM_LABELS",
    "RECALL_HALF_LIFE_DAYS",
    "RECALL_IMPORTANCE_DECAY",
    "RECALL_MIN_REL",
    "RECALL_POOL",
    "RECALL_W_FRESH",
    "RECALL_W_IMPORTANCE",
    "RECALL_W_REL",
    "SLOT_PATTERNS",
    "STOP_BIGRAMS",
    "STOP_CHARS",
    "_bm25_scores",
    "_bot_filter",
    "_clean",
    "_clock",
    "_cosine",
    "_freshness",
    "_from_vec",
    "GLUE_CHARS", "_informative_terms",
    "_jaccard",
    "_name_key",
    "_norm_key",
    "_now",
    "_parse_time_span",
    "_query_chars",
    "_recall_tokens",
    "_slot_facts",
    "_to_vec",
    "_unique_labels",
    "logger",
    "normalize_platform",
    "platform_label",
    "platform_of_umo",
]
