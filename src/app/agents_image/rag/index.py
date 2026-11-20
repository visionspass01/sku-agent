"""索引与融合召回：BM25（复用 `app.rag.bm25`）+ 轻量字符哈希向量，RRF(k=60) 融合。

第五轮 P1-1/P1-2 修复点：
- **查询改写**：`synonyms.rewrite_query()` 在检索前做业务同义词扩展；
- **稳定哈希**：向量桶由 `hashlib.blake2b` 决定，不再受 `PYTHONHASHSEED` 影响
  （过去同一 query 在重启/换进程后召回排序会漂移，离线评测也不可复现）；
- **相似度阈值**：`min_score` 过滤掉弱向量命中（默认 0 = 不过滤，避免误杀召回）；
- **轻量重排**：`rerank_backend=heuristic` 按「标题命中 → 词覆盖 → RRF 分」重排，
  不引入 cross-encoder 依赖（离线可跑）；
- **kind 过滤**：`kinds=` 可按语料类型检索（fix 语料只在 Critic 失败时用）。

为什么不接 Qdrant/embedding 服务：生图 Agent 要能在"只启用 image 模块"时离线启动，
知识库只有十几个片段，字符 n-gram 哈希向量做同义兜底 + 阈值/重排已足够；需要更强语义时
再换 provider（SKU 侧 `app/rag/vector_store.py` 已有现成实现可复用）。
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from dataclasses import dataclass

from app.agents_image.rag.corpus import Doc, corpus_fingerprint, load_corpus
from app.agents_image.rag.synonyms import rewrite_query

RRF_K = 60
_VEC_DIM = 256
RERANK_BACKENDS = ("heuristic", "none")


def _tokens(text: str) -> list[str]:
    """中英混合切分：中文按 1–2 字滑窗，英文按词。"""
    text = (text or "").lower()
    words = re.findall(r"[a-z0-9]+", text)
    chinese = re.findall(r"[\u4e00-\u9fff]", text)
    bigrams = [chinese[i] + chinese[i + 1] for i in range(len(chinese) - 1)]
    return words + chinese + bigrams


def _stable_bucket(token: str) -> int:
    """稳定桶号（P1-2）：blake2b 而不是内建 `hash()`，跨进程/重启结果一致。"""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % _VEC_DIM


def _vector(text: str) -> list[float]:
    """字符哈希向量（确定性、零依赖）：只用于召回兜底，不追求语义精度。"""
    vec = [0.0] * _VEC_DIM
    for token in _tokens(text):
        vec[_stable_bucket(token)] += 1.0
    norm = math.sqrt(sum(value * value for value in vec)) or 1.0
    return [value / norm for value in vec]


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


@dataclass
class Hit:
    doc: Doc
    score: float
    keyword_rank: int = 0
    vector_rank: int = 0
    rerank_score: float = 0.0

    @property
    def snippet(self) -> str:
        body = self.doc.body.replace("\n", " ").strip()
        return f"{self.doc.title}：{body[:160]}"

    def as_dict(self) -> dict:
        return {"doc_id": self.doc.doc_id, "kind": self.doc.kind, "title": self.doc.title,
                "score": round(self.score, 6), "rerank_score": round(self.rerank_score, 6),
                "snippet": self.snippet}


def _heuristic_rerank(hits: list[Hit], query: str) -> list[Hit]:
    """轻量重排（无模型）：标题命中 > 词覆盖 > RRF 分。

    生图场景"标题命中"的信号很强（`图位 1 主图`/`平台 淘系规则`/`修复：...`），
    而 RRF 只看名次、分辨不出"命中标题"与"命中正文某句"的差别。
    """
    query_tokens = [token for token in _tokens(query) if len(token) > 0]
    wanted = set(query_tokens)

    def rank_key(hit: Hit) -> tuple[float, float, float]:
        title_tokens = set(_tokens(hit.doc.title))
        body_tokens = set(_tokens(hit.doc.text))
        title_hit = 1.0 if title_tokens & wanted else 0.0
        coverage = (len(body_tokens & wanted) / len(wanted)) if wanted else 0.0
        return (title_hit, round(coverage, 6), hit.score)

    for hit in hits:
        title_hit, coverage, score = rank_key(hit)
        hit.rerank_score = round(title_hit * 1.0 + coverage, 6)
        hit.score = round(score + hit.rerank_score * 0.01, 6)
    hits.sort(key=lambda item: (item.rerank_score, item.score), reverse=True)
    return hits


class ImageIndex:
    """一个语料快照的索引（BM25 排名 + 向量排名 + RRF 融合 + 轻量重排）。"""

    def __init__(self, docs: list[Doc]) -> None:
        self.docs = list(docs)
        self.fingerprint = corpus_fingerprint(self.docs)
        self._tokens = [_tokens(doc.text) for doc in self.docs]
        self._vectors = [_vector(doc.text) for doc in self.docs]
        self._keyword = self._build_keyword()

    def _build_keyword(self):
        try:
            from app.rag.bm25 import KeywordIndex

            return KeywordIndex([doc.text for doc in self.docs])
        except Exception:  # noqa: BLE001 —— 语料检索是增强能力，缺依赖就只用向量
            return None

    def _keyword_scores(self, query: str) -> list[float]:
        if self._keyword is None or not self.docs:
            return [0.0] * len(self.docs)
        try:
            return list(self._keyword.scores(query))
        except Exception:  # noqa: BLE001
            return [0.0] * len(self.docs)

    def search(self, query: str, *, top_k: int = 5, kind: str | None = None,
               kinds: tuple[str, ...] | None = None, min_score: float = 0.0,
               rerank_backend: str = "heuristic", expand: bool = True) -> list[Hit]:
        """检索；`kinds` 可限定语料类型（`fix` 只在 Critic 失败时用）。"""
        text = (query or "").strip()
        if not self.docs or not text:
            return []
        expanded = rewrite_query(text) if expand else text
        wanted_kinds = set(kinds or ())
        allowed = [index for index, doc in enumerate(self.docs)
                   if (not kind or doc.kind == kind)
                   and (not wanted_kinds or doc.kind in wanted_kinds)]
        if not allowed:
            return []
        keyword = self._keyword_scores(expanded)
        query_vec = _vector(expanded)
        vector = [_cosine(query_vec, vec) for vec in self._vectors]

        def keep(index: int) -> bool:
            # 阈值只作用于"弱向量命中"：关键词命中或向量分达标才留（默认 0 = 全留）
            if keyword[index] > 0:
                return True
            return vector[index] >= float(min_score or 0.0)

        keyword_order = sorted((index for index in allowed if keep(index)),
                               key=lambda index: keyword[index], reverse=True)
        vector_order = sorted((index for index in allowed if keep(index)),
                              key=lambda index: vector[index], reverse=True)
        fused: dict[int, float] = {}
        for rank, index in enumerate(keyword_order):
            if keyword[index] > 0:
                fused[index] = fused.get(index, 0.0) + 1.0 / (RRF_K + rank + 1)
        for rank, index in enumerate(vector_order):
            if vector[index] > 0:
                fused[index] = fused.get(index, 0.0) + 1.0 / (RRF_K + rank + 1)
        hits = [Hit(doc=self.docs[index], score=score,
                    keyword_rank=keyword_order.index(index) + 1,
                    vector_rank=vector_order.index(index) + 1)
                for index, score in fused.items()]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        if str(rerank_backend or "heuristic").strip().lower() != "none":
            hits = _heuristic_rerank(hits, expanded)
        return hits[: max(1, int(top_k))]

    def status(self) -> dict:
        return {"docs": len(self.docs), "fingerprint": self.fingerprint,
                "kinds": sorted({doc.kind for doc in self.docs}),
                "keyword_index": self._keyword is not None}


_LOCK = threading.Lock()
_INDEX: ImageIndex | None = None
# 第五轮 P1-9：召回缓存走共享 TTL 缓存（Redis；未配置时 fakeredis 进程内自动降级），
# 命中率/后端都给面板看；进程内 dict 只作为"缓存层不可用"时的兜底。
_LOCAL_CACHE: dict[tuple, dict] = {}
_STATS = {"hits": 0, "misses": 0}


def _shared_cache():
    try:
        from app.cache.ttl_cache import get_ttl_cache

        return get_ttl_cache()
    except Exception:  # noqa: BLE001 —— image-only 模式或依赖缺失时退回进程内
        return None


def cache_backend() -> str:
    """`redis` / `fakeredis` / `memory`（缓存层不可用）。"""
    try:
        from app.cache.redis_factory import redis_mode

        return redis_mode() if _shared_cache() is not None else "memory"
    except Exception:  # noqa: BLE001
        return "memory"


def _cache_ttl() -> int:
    settings = _settings_for_retrieval()
    return max(1, int(getattr(settings, "rag_cache_ttl_seconds", 60) or 60))


def _hit_to_cache(hit: "Hit") -> dict:
    body = hit.doc.body.replace("\n", " ").strip()
    return {"doc_id": hit.doc.doc_id, "kind": hit.doc.kind, "title": hit.doc.title,
            "body": body[:400], "tags": list(hit.doc.tags),
            "score": hit.score, "rerank_score": hit.rerank_score,
            "keyword_rank": hit.keyword_rank, "vector_rank": hit.vector_rank}


def _hit_from_cache(item: dict) -> "Hit":
    doc = Doc(doc_id=str(item.get("doc_id") or ""), kind=str(item.get("kind") or ""),
              title=str(item.get("title") or ""), body=str(item.get("body") or ""),
              tags=list(item.get("tags") or []))
    return Hit(doc=doc, score=float(item.get("score") or 0.0),
               keyword_rank=int(item.get("keyword_rank") or 0),
               vector_rank=int(item.get("vector_rank") or 0),
               rerank_score=float(item.get("rerank_score") or 0.0))


def get_index(directory=None) -> ImageIndex:
    """进程内单例；语料指纹变了就重建（改 md 即生效，配合 admin reindex）。"""
    global _INDEX
    with _LOCK:
        docs = load_corpus(directory)
        fingerprint = corpus_fingerprint(docs) if docs else "empty"
        if _INDEX is None or _INDEX.fingerprint != fingerprint:
            _INDEX = ImageIndex(docs)
            _LOCAL_CACHE.clear()
        return _INDEX


def _settings_for_retrieval():
    try:
        from app.agents_image.config import get_image_settings

        return get_image_settings()
    except Exception:  # noqa: BLE001 —— 无配置时用保守默认
        return None


def retrieve(query: str, *, top_k: int = 5, kind: str | None = None,
             kinds: tuple[str, ...] | None = None, min_score: float | None = None,
             rerank_backend: str | None = None, use_cache: bool = True) -> list[Hit]:
    """检索 + 结果缓存（进程内；命中率给面板看）。

    默认读取 `IMAGE_RAG_MIN_SCORE` / `IMAGE_RAG_RERANK`，调用方可显式覆盖。
    """
    settings = _settings_for_retrieval()
    if min_score is None:
        min_score = float(getattr(settings, "rag_min_score", 0.0) or 0.0)
    if rerank_backend is None:
        rerank_backend = str(getattr(settings, "rag_rerank_backend", "heuristic")
                             or "heuristic")
    index = get_index()
    kinds_key = ",".join(sorted(kinds or ()))
    key = (index.fingerprint, query.strip(), int(top_k), kind or "", kinds_key,
           round(float(min_score or 0.0), 4), str(rerank_backend or ""))
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            _STATS["hits"] += 1
            return [_hit_from_cache(item) for item in cached]
    _STATS["misses"] += 1
    hits = index.search(query, top_k=top_k, kind=kind, kinds=kinds, min_score=min_score,
                        rerank_backend=rerank_backend)
    if use_cache:
        _cache_set(key, [_hit_to_cache(hit) for hit in hits])
    return hits


def _cache_key(key: tuple) -> str:
    raw = "|".join(str(part) for part in key)
    digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()
    return f"image:rag:{digest}"


def _cache_get(key: tuple):
    cache = _shared_cache()
    if cache is None:
        return _LOCAL_CACHE.get(key, {}).get("value")
    try:
        value = cache.get(_cache_key(key))
    except Exception:  # noqa: BLE001 —— 旁路缓存不许拖垮检索
        value = None
    return value


def _cache_set(key: tuple, value) -> None:
    cache = _shared_cache()
    if cache is None:
        _LOCAL_CACHE[key] = {"value": value}
        if len(_LOCAL_CACHE) > 512:              # 进程内兜底也不许无限涨
            for stale in list(_LOCAL_CACHE)[: len(_LOCAL_CACHE) - 512]:
                _LOCAL_CACHE.pop(stale, None)
        return
    try:
        cache.set(_cache_key(key), value, _cache_ttl())
    except Exception:  # noqa: BLE001
        _LOCAL_CACHE[key] = {"value": value}


def cache_stats() -> dict:
    total = _STATS["hits"] + _STATS["misses"]
    return {**_STATS, "total": total, "backend": cache_backend(),
            "hit_rate": round(_STATS["hits"] / total, 4) if total else 0.0}


def index_status() -> dict:
    index = get_index()
    return {**index.status(), "cache": cache_stats()}


def reset_index() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None
        _LOCAL_CACHE.clear()
        _STATS["hits"] = 0
        _STATS["misses"] = 0


__all__ = ["Hit", "ImageIndex", "get_index", "retrieve", "cache_backend", "cache_stats",
           "index_status", "reset_index", "RRF_K", "RERANK_BACKENDS", "_stable_bucket",
           "_vector"]
