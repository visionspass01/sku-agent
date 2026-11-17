"""向量后端等价性：Qdrant（哈希向量）与内存检索结果一致；向量库故障自动降级。

为什么重要：切换向量库不应该改变检索结论。语料只有 58 块，Qdrant 候选数 ≥ 语料数，
因此用同一份哈希向量时，两者的 top-k 应当完全一致；不一致就说明融合/过滤/候选
哪一环被改坏了（面试时这就是"我能证明换存储没换语义"的证据）。
"""

from __future__ import annotations

QUERIES = [
    "断码补全 尺码结构 怎么处理",
    "清仓折扣阶梯 最低几折",
    "补货量怎么测算 再订货点",
    "双11 报名门槛 动销率 断码率",
    "换季观察名单 距换季节点 30 天",
    "供应商交期 加急费用",
    "历史案例 大促断码 调拨",
    "女装羽绒服换季前多久启动首单",
]


def _rows_from_db(session):
    from app.db.models import KbChunk
    from app.rag.vectorizer import HashingVectorizer

    rows = []
    for chunk in session.query(KbChunk).order_by(KbChunk.doc_id, KbChunk.ord):
        rows.append({
            "chunk_id": chunk.chunk_id,
            "doc_id": chunk.doc_id,
            "text": chunk.text,
            "meta": dict(chunk.meta_json or {}),
            "vec": HashingVectorizer.from_bytes(chunk.vec or b""),
        })
    return rows


def _qdrant_retriever(tmp_path, monkeypatch, _settings, session):
    from app.rag import vector_store as vs
    from app.rag.embedding import get_embedding_provider
    from app.rag.hybrid import HybridRetriever
    from app.rag.index import sync_store

    monkeypatch.setattr(_settings, "vector_store", "qdrant")
    monkeypatch.setattr(_settings, "qdrant_url", f"local:{tmp_path}")
    monkeypatch.setattr(_settings, "qdrant_collection", "kb_parity")
    monkeypatch.setattr(_settings, "embedding_backend", "hash")
    vs.reset_store()
    store = vs.get_store()
    assert store is not None, "本地模式应能启动 Qdrant"
    sync_store(session)
    from app.db.models import KbChunk

    assert store.count() == session.query(KbChunk).count(), store.health()

    from app.rag.hybrid import ChunkRow

    rows = [
        ChunkRow(chunk_id=r["chunk_id"], doc_id=r["doc_id"], text=r["text"],
                 meta=r["meta"], vec=r["vec"])
        for r in _rows_from_db(session)
    ]
    return HybridRetriever(rows, store=store, embedder=get_embedding_provider())


def test_qdrant_matches_memory_backend(seeded, _settings, tmp_path, monkeypatch):
    from app.db.engine import session_scope
    from app.rag.hybrid import ChunkRow, HybridRetriever, rewrite_query

    with session_scope() as session:
        rows = [
            ChunkRow(chunk_id=r["chunk_id"], doc_id=r["doc_id"], text=r["text"],
                     meta=r["meta"], vec=r["vec"])
            for r in _rows_from_db(session)
        ]
        memory = HybridRetriever(rows)
        memory_hits = [
            [(h.chunk_id, h.doc_id) for h in memory.search(q, topk=5, rewritten=rewrite_query(q))]
            for q in QUERIES
        ]
        store_retriever = _qdrant_retriever(tmp_path, monkeypatch, _settings, session)
        qdrant_hits = [
            [(h.chunk_id, h.doc_id) for h in store_retriever.search(q, topk=5, rewritten=rewrite_query(q))]
            for q in QUERIES
        ]
    for q, mem, qd in zip(QUERIES, memory_hits, qdrant_hits):
        assert qd, q
        assert [d for _c, d in qd] == [d for _c, d in mem], f"{q}: {mem} vs {qd}"
    # 清理：恢复内存模式，避免影响其它测试
    from app.rag import vector_store as vs

    vs.reset_store()


def test_qdrant_failure_degrades_to_hash(seeded, _settings, tmp_path, monkeypatch):
    from app.rag import vector_store as vs
    from app.rag.hybrid import rewrite_query

    with session_scope_store() as session:
        qdrant = _qdrant_retriever(tmp_path, monkeypatch, _settings, session)

        class Boom:
            def search(self, *a, **k):
                raise RuntimeError("qdrant down")

            def count(self):
                return 0

        qdrant.store = Boom()
        hits = qdrant.search("清仓折扣阶梯", topk=3, rewritten=rewrite_query("清仓折扣阶梯"))
        assert hits, "向量库挂掉时仍应返回结果（降级哈希）"
        assert qdrant._store_failed_reason and "qdrant down" in qdrant._store_failed_reason
        assert vs.store_status()["degraded"] is True
    vs.reset_store()


def session_scope_store():
    from app.db.engine import session_scope

    return session_scope()
