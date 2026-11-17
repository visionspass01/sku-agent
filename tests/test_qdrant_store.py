"""Qdrant 向量库（本地模式，临时目录）：集合/写入/过滤/删除/重建。

不依赖 Docker：qdrant-client 的本地模式（path=...）与服务器模式同一套 API，
本机就能跑通并写测试；服务器上把 `QDRANT_URL` 换成 http://qdrant:6333 即可。
"""

from __future__ import annotations

import datetime as dt
import pathlib


def _store(tmp_path: pathlib.Path, dim: int = 8, collection: str = "kb_test"):
    from app.rag.vector_store import QdrantStore

    return QdrantStore(url=f"local:{tmp_path}", collection=collection, dim=dim)


def _vec(dim: int, seed: float) -> list[float]:
    return [((i + 1) * seed) % 1.0 for i in range(dim)]


def _points(dim: int) -> list[dict]:
    return [
        {"chunk_id": "sop-a#000", "vector": _vec(dim, 0.10),
         "payload": {"chunk_id": "sop-a#000", "doc_id": "sop-a", "ord": 0,
                     "department": "男装", "category": "男装", "season": "秋冬",
                     "effective_start": "2026-01-01", "effective_end": "2027-12-31",
                     "effective_start_ts": 20260101, "effective_end_ts": 20271231}},
        {"chunk_id": "sop-b#000", "vector": _vec(dim, 0.30),
         "payload": {"chunk_id": "sop-b#000", "doc_id": "sop-b", "ord": 0,
                     "department": "女装", "category": "女装", "season": "春夏",
                     "effective_start": "2026-01-01", "effective_end": "2027-12-31",
                     "effective_start_ts": 20260101, "effective_end_ts": 20271231}},
        {"chunk_id": "sop-c#000", "vector": _vec(dim, 0.90),
         "payload": {"chunk_id": "sop-c#000", "doc_id": "sop-c", "ord": 0,
                     "department": "通用", "category": "全部", "season": "全部",
                     "effective_start": "2026-01-01", "effective_end": "2027-12-31",
                     "effective_start_ts": 20260101, "effective_end_ts": 20271231}},
    ]


def test_qdrant_upsert_count_and_search(tmp_path):
    store = _store(tmp_path)
    try:
        assert store.count() == 0
        assert store.upsert(_points(8)) == 3
        assert store.count() == 3
        hits = store.search(_vec(8, 0.30), topk=2)
        assert hits and hits[0][0] == "sop-b#000", hits
        assert all(isinstance(score, float) for _chunk, score in hits)
    finally:
        store.close()


def test_qdrant_filters_department_and_as_of(tmp_path):
    store = _store(tmp_path)
    try:
        store.upsert(_points(8))
        men = store.search(_vec(8, 0.5), topk=5, filters={"department": ["男装", "通用"]})
        doc_ids = {chunk.split("#")[0] for chunk, _score in men}
        assert "sop-b" not in doc_ids, doc_ids
        assert {"sop-a", "sop-c"} <= doc_ids, doc_ids

        # as_of 早于生效期 → 无结果（与 Python 侧 _passes_filter 口径一致）
        early = store.search(_vec(8, 0.5), topk=5, as_of=dt.date(2024, 6, 1))
        assert early == []
        ok = store.search(_vec(8, 0.5), topk=5, as_of=dt.date(2026, 6, 1))
        assert len(ok) == 3
    finally:
        store.close()


def test_qdrant_delete_doc(tmp_path):
    store = _store(tmp_path)
    try:
        store.upsert(_points(8))
        store.delete_doc("sop-b")
        assert store.count() == 2
    finally:
        store.close()


def test_qdrant_collection_name_carries_dim(tmp_path):
    """集合名带维度：换 embedding 模型 = 换集合，不做破坏性重建（旧集合可手工删）。"""
    store8 = _store(tmp_path, dim=8)
    try:
        store8.upsert(_points(8))
        assert store8.collection == "kb_test_8"
        assert store8.count() == 3
    finally:
        store8.close()
    store16 = _store(tmp_path, dim=16)
    try:
        assert store16.collection == "kb_test_16"
        assert store16.count() == 0
        assert store16.health()["dim"] == 16
    finally:
        store16.close()


def test_store_disabled_returns_none(_settings, monkeypatch):
    from app.rag import vector_store as vs

    monkeypatch.setattr(_settings, "vector_store", "memory")
    monkeypatch.setattr(_settings, "qdrant_url", "")
    vs.reset_store()
    assert vs.get_store() is None
    status = vs.store_status()
    assert status["backend"] == "memory" and status["degraded"] is False


def test_store_url_missing_marks_degraded(_settings, monkeypatch):
    from app.rag import vector_store as vs

    monkeypatch.setattr(_settings, "vector_store", "qdrant")
    monkeypatch.setattr(_settings, "qdrant_url", "")
    vs.reset_store()
    assert vs.get_store() is None
    status = vs.store_status()
    assert status["degraded"] is True
    assert "QDRANT_URL" in status["degraded_reason"]
    vs.reset_store()


def test_unreachable_qdrant_falls_back_not_raise(_settings, monkeypatch):
    from app.rag import vector_store as vs

    monkeypatch.setattr(_settings, "vector_store", "qdrant")
    monkeypatch.setattr(_settings, "qdrant_url", "http://127.0.0.1:9")  # 必然拒绝连接
    monkeypatch.setattr(_settings, "qdrant_timeout_seconds", 0.5)
    vs.reset_store()
    assert vs.get_store() is None
    assert vs.store_status()["degraded"] is True
    vs.reset_store()
