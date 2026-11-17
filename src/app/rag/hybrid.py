"""混合召回（RAG 链路第 4-5 步）：RRF 融合 + 无答案判定 + 重排 + 多样性。

相比旧实现（逐查询 min-max 归一化后 0.6×向量 + 0.4×关键词，再启发式加分）改了三处：

1. **RRF 融合**（评审 3.4-1）：`score = Σ 1/(k + rank_i)`（k=60）。
   min-max 归一化有个致命副作用：不管文档多不相关，最高分总被拉到 1.0、最低拉到 0.0，
   于是**永远返回 top-k、永远不说"不知道"**。RRF 只看名次，天然免疫分数量纲问题，
   也不会把「两路都排第 20」的块抬到比「一路第 1」更高。
2. **无答案判定**（评审 3.4-2）：BM25 完全没命中（`max_bm25 == 0`）且向量相似度低于
   `RAG_MIN_VECTOR_SCORE` 时，返回空结果——让上层能说"知识库没有相关内容"，
   而不是硬塞 5 条 SOP 让模型编。这是可信度问题，不是精度问题。
3. **多样性**（评审 3.4-8）：先按同文档最多 `RAG_MAX_PER_DOC` 条截断，再做 MMR 去重。
   旧实现里同一篇 SOP 的连续 3 个块可以占满 top-5，等于只召回了一篇文档。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re

import numpy as np

from app.observability import latency
from app.rag.bm25 import KeywordIndex, tokenize_zh
from app.rag.vectorizer import DIM, HashingVectorizer, cosine

logger = logging.getLogger("sku-agent.rag")

# 查询改写用的业务同义词（生产可由中台 /metadata/synonyms 覆盖，见 app/rag/synonyms.py）
QUERY_SYNONYMS = {
    "断码": ["缺码", "尺码断货", "尺码不足"],
    "补货": ["订货", "采购", "补单"],
    "清仓": ["清库", "甩货", "折扣清仓"],
    "积压": ["滞销", "库存积压"],
    "退货率": ["退货", "售后率"],
    "大促": ["双11", "618", "年货节", "活动"],
    "换季": ["季末", "季节切换"],
    "缺货": ["断货", "无货"],
    "调拨": ["仓间调拨", "转仓"],
    "交期": ["到货时间", "供应商交期"],
    "齐码": ["全码", "码数齐全"],
    "生命周期": ["上新", "主推", "清仓", "长尾"],
}


def rewrite_query(query: str, synonyms: dict | None = None) -> str:
    """轻量查询改写：补业务同义词，提升中文 BM25 召回（确定性、零成本）。"""
    table = synonyms if synonyms is not None else QUERY_SYNONYMS
    q = (query or "").strip()
    extra: list[str] = []
    for key, syns in table.items():
        if key in q:
            extra.extend(syns)
    if extra:
        q = q + " " + " ".join(extra)
    return q


@dataclasses.dataclass
class Hit:
    chunk_id: str
    doc_id: str
    text: str
    score: float
    vector_score: float
    keyword_score: float
    meta: dict
    rerank_score: float = 0.0
    rerank_backend: str = ""
    rerank_degraded: bool = False

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "title": self.meta.get("title", ""),
            "section": self.meta.get("section", ""),
            "text": self.text,
            "score": round(self.score, 6),
            "vector_score": round(self.vector_score, 4),
            "keyword_score": round(self.keyword_score, 4),
            "rerank_score": round(self.rerank_score, 4),
            "rerank_backend": self.rerank_backend,
            "rerank_degraded": self.rerank_degraded,
            "meta": self.meta,
        }


@dataclasses.dataclass
class ChunkRow:
    chunk_id: str
    doc_id: str
    text: str
    meta: dict
    vec: np.ndarray


class HybridRetriever:
    def __init__(self, rows: list[ChunkRow], store=None, embedder=None) -> None:
        self.rows = rows
        self.vec_mat = (np.stack([r.vec for r in rows], axis=0)
                        if rows else np.zeros((0, DIM), dtype=np.float32))
        # 字段加权索引：标题/小节标题/正文分开算 BM25
        self.kw = KeywordIndex([
            {"title": r.meta.get("title", ""), "section": r.meta.get("section", ""),
             "body": r.meta.get("body") or r.text}
            for r in rows
        ]) if rows else None
        self.store = store
        self.embedder = embedder
        self.vectorizer = HashingVectorizer()
        self._store_failed_reason = ""
        self._tokens = [set(tokenize_zh(r.text)) for r in rows]

    # ------------------------------------------------------------ 过滤
    def _passes_filter(self, meta: dict, filters: dict | None, as_of: dt.date | None) -> bool:
        if filters:
            # department 支持「字符串或列表」：列表用于「本部门 + 通用」的可见性控制
            for key in ("department", "category", "season", "warehouse", "platform"):
                want = filters.get(key)
                if not want:
                    continue
                allowed = want if isinstance(want, (list, tuple, set)) else [want]
                if "全部" in allowed:
                    allowed = [a for a in allowed if a != "全部"]
                    if not allowed:
                        continue
                got = meta.get(key) or ("通用" if key == "department" else "全部")
                if got not in allowed and got != "全部":
                    return False
        if as_of:
            start = meta.get("effective_start")
            end = meta.get("effective_end")
            if start and dt.date.fromisoformat(start) > as_of:
                return False
            if end and dt.date.fromisoformat(end) < as_of:
                return False
        return True

    # ------------------------------------------------------------ 两路召回
    def _vector_scores(self, queries: list[str], filters: dict | None, as_of: dt.date | None,
                       idxs: list[int]) -> np.ndarray:
        if self.store is not None and self.embedder is not None:
            try:
                return self._store_scores(queries, filters, as_of, idxs)
            except Exception as exc:  # noqa: BLE001 —— 向量库/embedding 失败只降级
                from app.rag import vector_store as vs

                self._store_failed_reason = f"{type(exc).__name__}: {exc}"
                vs.mark_degraded(self._store_failed_reason)
                logger.warning("向量检索失败，降级为本地哈希余弦：%s", self._store_failed_reason)
        return self._hash_scores(queries, idxs)

    def _hash_scores(self, queries: list[str], idxs: list[int]) -> np.ndarray:
        """本地哈希向量余弦分：**矩阵乘**，不要逐行调 cosine()。

        向量都已 L2 归一化，所以 `Q @ D.T` 直接就是余弦。1200 篇（≈5700 块）时，
        逐行 Python 循环要 500ms+，改成矩阵乘后是毫秒级——这是"能不能抗住规模"的分水岭。
        """
        if not idxs:
            return np.zeros(0, dtype=np.float32)
        sub_mat = self.vec_mat[idxs]
        vec_q = np.stack([self.vectorizer.embed(q) for q in queries], axis=0)
        return np.asarray((vec_q @ sub_mat.T).max(axis=0), dtype=np.float32)

    def _store_scores(self, queries: list[str], filters: dict | None, as_of: dt.date | None,
                      idxs: list[int]) -> np.ndarray:
        from app.config import get_settings

        settings = get_settings()
        limit = max(int(settings.qdrant_candidate_n or 64), 8)
        score_by_chunk: dict[str, float] = {}
        for query in queries:
            vector = self.embedder.embed_one(query)
            for chunk_id, score in self.store.search(vector, limit, filters=filters, as_of=as_of):
                if score > score_by_chunk.get(chunk_id, -1.0):
                    score_by_chunk[chunk_id] = float(score)
        return np.array([score_by_chunk.get(self.rows[i].chunk_id, 0.0) for i in idxs])

    # ------------------------------------------------------------ 融合
    def search(
        self,
        query: str,
        topk: int = 5,
        filters: dict | None = None,
        as_of: dt.date | None = None,
        rewritten: str | None = None,
    ) -> list[Hit]:
        """同步检索（不含 LLM 重排）。需要真重排请用 `app.rag.pipeline.search_kb`。"""
        started = __import__("time").perf_counter()
        try:
            return self._search(query, topk, filters, as_of, rewritten)
        finally:
            latency.record("rag", (__import__("time").perf_counter() - started) * 1000)

    def candidates(
        self,
        query: str,
        *,
        filters: dict | None = None,
        as_of: dt.date | None = None,
        rewritten: str | None = None,
        pool: int = 30,
        rrf_k: int = 60,
        max_per_doc: int = 4,
        mode: str = "rrf",
    ) -> tuple[list[dict], dict]:
        """返回融合后的候选（未重排、未截断）+ 诊断信息（给 pipeline 做重排与无答案判定）。"""
        from app.config import get_settings

        settings = get_settings()
        queries = [q for q in [query, rewritten] if q]
        if not self.rows or not queries:
            return [], {"reason": "empty_corpus_or_query"}

        idxs = [i for i, r in enumerate(self.rows)
                if self._passes_filter(r.meta, filters, as_of)]
        if not idxs:
            return [], {"reason": "filtered_out"}

        kw_scores = np.zeros(len(self.rows))
        for q in queries:
            scores = np.array(self.kw.scores(q)) if self.kw else np.zeros(len(self.rows))
            kw_scores = np.maximum(kw_scores, scores)
        kw_sub = kw_scores[idxs]
        vec_scores = self._vector_scores(queries, filters, as_of, idxs)

        # ---- 无答案判定（评审 3.4-2）----
        # RRF 只按名次融合，**无法**回答"这条查询到底有没有答案"：哪怕全是垃圾，
        # 也总有一个块排第 1。所以另用**绝对**信号判断，核心是"最像的那个块有多像"：
        #   max_vec  （余弦相似度）——最强、最稳的信号
        #   coverage （查询词被覆盖比例）——兜底，避免哈希向量偶然撞出高分
        #   max_kw   （BM25 最高分）——反方向：词法证据很强时**不**弃答
        # 阈值在本地哈希向量 + 当前 14 篇语料上标定（正样本最低 vec=0.276 / cov=0.10；
        # 负样本最高 vec=0.236 / cov=0.133）。换 embedding 或语料大改后
        # 必须重跑 `eval_rag --negatives` 重新标定——脚本里的 no_answer_rate 就是为此存在。
        max_kw = float(kw_sub.max()) if len(kw_sub) else 0.0
        max_vec = float(vec_scores.max()) if len(vec_scores) else 0.0
        min_vec = float(getattr(settings, "rag_min_vector_score", 0.25))
        min_cov = float(getattr(settings, "rag_min_coverage", 0.25))
        strong_kw = float(getattr(settings, "rag_min_keyword_score", 15.0))
        if max_vec < min_vec and max_kw < strong_kw:
            best_pos = int(np.argmax(vec_scores)) if len(vec_scores) else 0
            coverage = _coverage(queries[-1], self.rows[idxs[best_pos]], self._tokens[idxs[best_pos]])
            if coverage < min_cov:
                return [], {"reason": "no_answer", "max_keyword": round(max_kw, 4),
                            "max_vector": round(max_vec, 4), "coverage": round(coverage, 4),
                            "thresholds": {"vector": min_vec, "coverage": min_cov,
                                           "strong_keyword": strong_kw}}

        # RRF：只按名次融合，不看原始分数量纲
        kw_rank = _rank_map(kw_sub)
        vec_rank = _rank_map(vec_scores)
        fused: list[dict] = []
        # 消融实验用：只保留一路（bm25 / vector），默认 rrf 两路融合
        use_kw = mode in ("rrf", "bm25")
        use_vec = mode in ("rrf", "vector")
        for pos, i in enumerate(idxs):
            row = self.rows[i]
            score = 0.0
            kw_pos = kw_rank.get(pos) if use_kw else None
            vec_pos = vec_rank.get(pos) if use_vec else None
            if kw_pos is not None:
                score += 1.0 / (rrf_k + kw_pos)
            if vec_pos is not None:
                score += 1.0 / (rrf_k + vec_pos)
            fused.append({
                "chunk_id": row.chunk_id, "doc_id": row.doc_id, "text": row.text,
                "meta": row.meta, "score": score,
                "vector_score": float(vec_scores[pos]), "keyword_score": float(kw_sub[pos]),
                "_tokens": self._tokens[i], "_index": i,
            })
        fused.sort(key=lambda h: h["score"], reverse=True)
        # 候选池也要做**宽松的**同文档配额：一篇长 SOP 的 8 个块在 RRF 里可能占满 top-15，
        # 导致真正该命中的短文档进不了候选池（重排再准也救不回来）。
        # 这里只是让候选覆盖更多文档，严格的多样性在最终 top-k 上再做。
        pool_cap = max(2, max_per_doc * 2)
        per_doc: dict[str, int] = {}
        spread: list[dict] = []
        overflow: list[dict] = []
        for item in fused:
            doc = item["doc_id"]
            if per_doc.get(doc, 0) < pool_cap:
                per_doc[doc] = per_doc.get(doc, 0) + 1
                spread.append(item)
            else:
                overflow.append(item)
        pool_items = (spread + overflow)[:max(pool, 1)]
        return pool_items, {
            "reason": "ok", "max_keyword": round(max_kw, 4), "max_vector": round(max_vec, 4),
            "fused": len(fused), "pool": len(pool_items), "rrf_k": rrf_k, "mode": mode,
            "documents": len({item["doc_id"] for item in pool_items}),
            "store_degraded": self._store_failed_reason,
        }

    def _search(
        self,
        query: str,
        topk: int,
        filters: dict | None,
        as_of: dt.date | None,
        rewritten: str | None,
    ) -> list[Hit]:
        from app.config import get_settings

        settings = get_settings()
        max_per_doc = max(1, int(getattr(settings, "rag_max_per_doc", 2)))
        fused, _diag = self.candidates(query, filters=filters, as_of=as_of,
                                       rewritten=rewritten, pool=topk * 3,
                                       max_per_doc=max_per_doc)
        if not fused:
            return []
        picked = self._diversify(fused, topk, max_per_doc=max_per_doc,
                                 mmr_lambda=float(getattr(settings, "rag_mmr_lambda", 0.7)))
        return [_to_hit(item) for item in picked]

    # ------------------------------------------------------------ 多样性
    def _diversify(self, items: list[dict], topk: int, *, max_per_doc: int,
                   mmr_lambda: float) -> list[dict]:
        """同文档配额 + MMR（用块向量做相似度，避免同一段话的相邻块占满 top-k）。"""
        per_doc: dict[str, int] = {}
        capped: list[dict] = []
        for item in items:
            doc = item["doc_id"]
            if per_doc.get(doc, 0) >= max_per_doc:
                continue
            per_doc[doc] = per_doc.get(doc, 0) + 1
            capped.append(item)
            if len(capped) >= topk * 3:
                break
        if len(capped) <= topk:
            return capped[:topk]

        # MMR 的相关度必须先归一化到 [0,1]：RRF 分数是 ~1e-2 量级（1/(60+rank)），
        # 而余弦相似度是 ~1e-1 量级。直接混合会让「去重项」压倒「相关度项」——
        # 表现为把排名第 2 的正确答案换成排名第 9 的边缘文档（实测踩到过）。
        max_rel = max((float(item["score"]) for item in capped), default=0.0) or 1.0
        selected: list[dict] = []
        pool = list(capped)
        while pool and len(selected) < topk:
            if not selected:
                selected.append(pool.pop(0))
                continue
            best_idx, best_value = 0, float("-inf")
            for idx, item in enumerate(pool):
                rel = float(item["score"]) / max_rel
                sim = max(self._similarity(item, chosen) for chosen in selected)
                value = mmr_lambda * rel - (1 - mmr_lambda) * sim
                if value > best_value:
                    best_idx, best_value = idx, value
            selected.append(pool.pop(best_idx))
        return selected

    def _similarity(self, a: dict, b: dict) -> float:
        ia, ib = a.get("_index"), b.get("_index")
        if ia is None or ib is None or self.vec_mat.shape[0] == 0:
            return 0.0
        return cosine(self.vec_mat[ia], self.vec_mat[ib])


def _coverage(query: str, row: "ChunkRow", row_tokens: set[str] | None = None) -> float:
    """查询词被该块覆盖的比例（去掉「的/了/是什么」这类无信息词再算）。"""
    stop = {"的", "了", "是", "在", "和", "吗", "几", "呢", "有", "要", "怎么", "如何",
            "什么", "多少", "哪", "个", "我", "你", "他", "它", "这", "那"}
    q_tokens = {t for t in tokenize_zh(query) if t not in stop and len(t) > 1}
    if not q_tokens:
        return 0.0
    tokens = row_tokens if row_tokens is not None else set(tokenize_zh(row.text))
    return len(q_tokens & tokens) / max(1, len(q_tokens))


def _rank_map(scores: np.ndarray) -> dict[int, int]:
    """位置 → 名次（1 起）。分数为 0 的视为「未命中」，不给名次。"""
    order = sorted(range(len(scores)), key=lambda i: float(scores[i]), reverse=True)
    out: dict[int, int] = {}
    rank = 0
    for pos in order:
        if float(scores[pos]) <= 0.0:
            continue
        rank += 1
        out[pos] = rank
    return out


def _to_hit(item: dict) -> Hit:
    return Hit(
        chunk_id=item["chunk_id"], doc_id=item["doc_id"], text=item["text"],
        score=float(item["score"]), vector_score=float(item["vector_score"]),
        keyword_score=float(item["keyword_score"]), meta=item["meta"],
        rerank_score=float(item.get("rerank_score") or 0.0),
        rerank_backend=str(item.get("rerank_backend") or ""),
        rerank_degraded=bool(item.get("rerank_degraded")),
    )


def hit_from_candidate(item: dict, *, backend: str = "", degraded: bool = False) -> Hit:
    """把 pipeline 的候选 dict 转成对外的 Hit（带上重排信息）。"""
    hit = _to_hit(item)
    hit.rerank_backend = backend
    hit.rerank_degraded = degraded
    return hit


_VERSION_RE = re.compile(r"v(\d+)(?:\.(\d+))?")


def _version_boost(version: str) -> float:
    m = _VERSION_RE.search(version or "")
    if not m:
        return 0.0
    major = int(m.group(1))
    minor = int(m.group(2) or 0)
    return min(0.03, (major * 100 + minor) / 100000.0)


_retriever: HybridRetriever | None = None


def get_retriever(session) -> HybridRetriever:  # noqa: ANN001
    """从库中装载全部 chunk 构建检索器（进程内单例，索引重建后失效）。"""
    global _retriever
    if _retriever is not None:
        return _retriever
    from app.db.models import KbChunk

    rows: list[ChunkRow] = []
    for chunk in session.query(KbChunk).order_by(KbChunk.doc_id, KbChunk.ord):
        meta = dict(chunk.meta_json or {})
        vec = HashingVectorizer.from_bytes(chunk.vec or b"")
        rows.append(ChunkRow(chunk_id=chunk.chunk_id, doc_id=chunk.doc_id,
                             text=chunk.text, meta=meta, vec=vec))

    store = None
    embedder = None
    try:
        from app.rag import vector_store as vs

        store = vs.get_store()
        if store is not None:
            from app.rag.embedding import get_embedding_provider

            embedder = get_embedding_provider()
    except Exception as exc:  # noqa: BLE001 —— 扩展组件初始化失败不影响基础检索
        logger.warning("向量库/embedding 初始化失败，使用内存检索：%s", exc)
        store, embedder = None, None
    _retriever = HybridRetriever(rows, store=store, embedder=embedder)
    return _retriever


def invalidate_retriever() -> None:
    global _retriever
    _retriever = None


def retriever_status() -> dict:
    if _retriever is None:
        return {"loaded": False}
    return {"loaded": True, "chunks": len(_retriever.rows),
            "backend": "qdrant" if _retriever.store is not None else "memory",
            "store_degraded": bool(_retriever._store_failed_reason)}
