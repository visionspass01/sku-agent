"""索引与融合召回：BM25（复用 `app.rag.bm25`）+ 轻量字符哈希向量，RRF(k=60) 融合。

为什么不用 Qdrant/embedding 服务：生图 Agent 要能在"只启用 image 模块"时离线启动，
知识库只有十几个片段，字符 n-gram 哈希向量足够做同义兜底；需要更强语义时再换 provider。
"""

from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass

from app.agents_image.rag.corpus import Doc, corpus_fingerprint, load_corpus

RRF_K = 60
_VEC_DIM = 256


def _tokens(text: str) -> list[str]:
    """中英混合切分：中文按 1–2 字滑窗，英文按词。"""
    text = (text or "").lower()
    words = re.findall(r"[a-z0-9]+", text)
    chinese = re.findall(r"[\u4e00-\u9fff]", text)
    bigrams = [chinese[i] + chinese[i + 1] for i in range(len(chinese) - 1)]
    return words + chinese + bigrams


def _vector(text: str) -> list[float]:
    """字符哈希向量（确定性、零依赖）：只用于召回兜底，不追求语义精度。"""
    vec = [0.0] * _VEC_DIM
    for token in _tokens(text):
        bucket = hash(token) % _VEC_DIM
        vec[bucket] += 1.0
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

    @property
    def snippet(self) -> str:
        body = self.doc.body.replace("\n", " ").strip()
        return f"{self.doc.title}：{body[:160]}"

    def as_dict(self) -> dict:
        return {"doc_id": self.doc.doc_id, "kind": self.doc.kind, "title": self.doc.title,
                "score": round(self.score, 6), "snippet": self.snippet}


class ImageIndex:
    """一个语料快照的索引（BM25 排名 + 向量排名 + RRF 融合）。"""

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

    def search(self, query: str, *, top_k: int = 5, kind: str | None = None) -> list[Hit]:
        if not self.docs or not (query or "").strip():
            return []
        allowed = [index for index, doc in enumerate(self.docs)
                   if not kind or doc.kind == kind]
        if not allowed:
            return []
        keyword = self._keyword_scores(query)
        query_vec = _vector(query)
        vector = [_cosine(query_vec, vec) for vec in self._vectors]

        keyword_order = sorted(allowed, key=lambda index: keyword[index], reverse=True)
        vector_order = sorted(allowed, key=lambda index: vector[index], reverse=True)
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
        return hits[: max(1, int(top_k))]

    def status(self) -> dict:
        return {"docs": len(self.docs), "fingerprint": self.fingerprint,
                "kinds": sorted({doc.kind for doc in self.docs}),
                "keyword_index": self._keyword is not None}


_LOCK = threading.Lock()
_INDEX: ImageIndex | None = None
_CACHE: dict[tuple[str, str, int, str], list[Hit]] = {}
_STATS = {"hits": 0, "misses": 0}


def get_index(directory=None) -> ImageIndex:
    """进程内单例；语料指纹变了就重建（改 md 即生效，配合 admin reindex）。"""
    global _INDEX
    with _LOCK:
        docs = load_corpus(directory)
        fingerprint = corpus_fingerprint(docs) if docs else "empty"
        if _INDEX is None or _INDEX.fingerprint != fingerprint:
            _INDEX = ImageIndex(docs)
            _CACHE.clear()
        return _INDEX


def retrieve(query: str, *, top_k: int = 5, kind: str | None = None,
             use_cache: bool = True) -> list[Hit]:
    """检索 + 结果缓存（同一 query+kind 在进程内命中缓存，命中率给面板看）。"""
    index = get_index()
    key = (index.fingerprint, query.strip(), int(top_k), kind or "")
    if use_cache and key in _CACHE:
        _STATS["hits"] += 1
        return _CACHE[key]
    _STATS["misses"] += 1
    hits = index.search(query, top_k=top_k, kind=kind)
    _CACHE[key] = hits
    return hits


def recall_fragments(queries: list[str], *, top_k: int = 2) -> dict[str, list[str]]:
    """把多个查询串的召回结果合并去重（compose 时按图位取用）。"""
    seen: dict[str, list[str]] = {}
    for query in queries:
        hits = retrieve(query, top_k=top_k)
        seen[query] = [hit.snippet for hit in hits]
    return seen


def cache_stats() -> dict:
    total = _STATS["hits"] + _STATS["misses"]
    return {**_STATS, "total": total,
            "hit_rate": round(_STATS["hits"] / total, 4) if total else 0.0}


def index_status() -> dict:
    index = get_index()
    return {**index.status(), "cache": cache_stats()}


def reset_index() -> None:
    global _INDEX
    with _LOCK:
        _INDEX = None
        _CACHE.clear()
        _STATS["hits"] = 0
        _STATS["misses"] = 0


__all__ = ["Hit", "ImageIndex", "get_index", "retrieve", "recall_fragments",
           "index_status", "cache_stats", "reset_index", "RRF_K"]
