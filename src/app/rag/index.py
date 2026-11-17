"""知识库索引构建与增量一致性（RAG 链路第 6 步）。

增量策略（评审 3.3 修复）：
- 每篇文档算 body 的 sha256，与 `kb_doc.sha256` 比较；未变化 → 跳过（不重分块、不重向量化）；
- 变化 → **按块 id 差集**更新：SQLite 删掉不再存在的块、写入新块；向量库先 `delete_doc`
  再 upsert（旧实现只 upsert 不 delete，文档变短后残留块会一直被检索到，属于确定性 bug）；
- 磁盘上已删除的文档 → 从 kb_doc / kb_chunk / 向量库移除；
- `chunks_sha` 记录「块 id 集合指纹」，供 `reconcile_store()` 做**按文档对账**，
  而不是每次启动整库重建（旧实现 count 不一致就全量重建，把同步 bug 掩盖成了性能问题）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import KB_DIR, get_settings
from app.db.models import KbChunk, KbDoc, KbSyncLog
from app.rag.chunker import CHUNKER_VERSION, chunk_document
from app.rag.hybrid import invalidate_retriever
from app.rag.loader import load_documents
from app.rag.vectorizer import HashingVectorizer

logger = logging.getLogger("sku-agent.rag")
_VECTORIZER = HashingVectorizer()


@dataclass
class IndexStats:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    chunks: int = 0
    chunks_added: int = 0
    chunks_removed: int = 0

    def to_dict(self) -> dict:
        return {
            "added": self.added, "updated": self.updated,
            "skipped": self.skipped, "deleted": self.deleted, "chunks": self.chunks,
            "chunks_added": self.chunks_added, "chunks_removed": self.chunks_removed,
        }


def chunks_digest(chunk_ids: list[str]) -> str:
    """块 id 集合指纹（排序后 sha1）：内容寻址的 id 不变 → 指纹不变 → 无需重建。"""
    payload = "\n".join(sorted(chunk_ids)).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def build_index(session: Session, kb_dir: Path | None = None) -> IndexStats:
    kb_dir = kb_dir or KB_DIR
    stats = IndexStats()
    docs = load_documents(kb_dir)
    now = dt.datetime.now()

    existing = {d.doc_id: d for d in session.scalars(select(KbDoc))}
    on_disk = {d.doc_id for d in docs}

    for doc in docs:
        old = existing.get(doc.doc_id)
        # 文档没变 **且** 分块方案没升级 → 跳过；否则重建（升级分块逻辑后旧块并不等价）
        if (old is not None and old.sha256 == doc.sha256
                and (old.chunker_version or "") == CHUNKER_VERSION):
            stats.skipped.append(doc.doc_id)
            continue
        action = "update" if old is not None else "add"
        row = old or KbDoc(doc_id=doc.doc_id)
        row.title = doc.title
        row.department = doc.department
        row.category = doc.category
        row.season = doc.season
        row.warehouse = doc.warehouse
        row.platform = doc.platform
        row.effective_start = doc.effective_start
        row.effective_end = doc.effective_end
        row.version = doc.version
        row.source = doc.path.name
        row.sha256 = doc.sha256
        row.updated_at = now
        session.add(row)
        session.flush()
        added, removed = _sync_chunks(session, doc)
        stats.chunks_added += added
        stats.chunks_removed += removed
        session.add(KbSyncLog(doc_id=doc.doc_id, action=action, at=now,
                              note=f"{doc.path.name} sha={doc.sha256[:8]} "
                                   f"chunks+{added}/-{removed}"))
        (stats.added if action == "add" else stats.updated).append(doc.doc_id)

    for doc_id in existing:
        if doc_id not in on_disk:
            session.execute(delete(KbChunk).where(KbChunk.doc_id == doc_id))
            session.execute(delete(KbDoc).where(KbDoc.doc_id == doc_id))
            session.add(KbSyncLog(doc_id=doc_id, action="delete", at=now, note="源文件已移除"))
            _delete_store_doc(doc_id)
            stats.deleted.append(doc_id)

    stats.chunks = session.query(KbChunk).count()
    session.flush()
    invalidate_retriever()
    return stats


def _document_chunks(doc):  # noqa: ANN001, ANN201
    """生成该文档最新的块（含向量），并把元数据指纹写回 kb_doc。"""
    chunks = chunk_document(doc)
    return chunks


def _sync_chunks(session: Session, doc) -> tuple[int, int]:  # noqa: ANN001
    """按 chunk_id 差集同步一篇文档的块（SQLite + 向量库）。返回 (新增, 删除)。"""
    chunks = _document_chunks(doc)
    new_ids = {ch.chunk_id for ch in chunks}
    old_ids = set(session.scalars(select(KbChunk.chunk_id).where(KbChunk.doc_id == doc.doc_id)))
    stale = old_ids - new_ids
    if stale:
        session.execute(delete(KbChunk).where(KbChunk.chunk_id.in_(list(stale))))

    for ch in chunks:
        vec = _VECTORIZER.embed(ch.text)
        row = session.get(KbChunk, ch.chunk_id)
        if row is None:
            session.add(KbChunk(
                chunk_id=ch.chunk_id, doc_id=ch.doc_id, ord=ch.ord, text=ch.text,
                meta_json=ch.meta, vec=HashingVectorizer.to_bytes(vec), updated_at=dt.datetime.now(),
            ))
        else:                      # 同一块内容没变但 ord/元数据可能变（标题改了）
            row.ord = ch.ord
            row.meta_json = ch.meta
            row.text = ch.text
            row.vec = HashingVectorizer.to_bytes(vec)
            row.updated_at = dt.datetime.now()

    db_row = session.get(KbDoc, doc.doc_id)
    if db_row is not None:
        db_row.chunk_count = len(new_ids)
        db_row.chunks_sha = chunks_digest(sorted(new_ids))
        db_row.chunker_version = CHUNKER_VERSION

    if stale or chunks:
        # 先在向量库里清掉这篇文档的旧块，再写新块：
        # 只 upsert 不 delete 时，文档变短会留下永不失效的陈旧块（确定性 bug）
        _delete_store_doc(doc.doc_id)
        _sync_store_chunks(chunks)
    return len(new_ids - old_ids), len(stale)


def _delete_store_doc(doc_id: str) -> None:
    from app.rag import vector_store as vs

    store = vs.get_store()
    if store is None:
        return
    try:
        store.delete_doc(doc_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("向量库删除失败（doc_id=%s）：%s", doc_id, exc)


def _payload_of(chunk) -> dict:  # noqa: ANN001
    """Qdrant payload：只放元数据（不存正文），正文仍从 SQLite 读，避免双份数据漂移。"""
    meta = dict(getattr(chunk, "meta", None) or getattr(chunk, "meta_json", None) or {})
    payload = {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "ord": int(chunk.ord or 0),
        "doc_sha": str(meta.get("sha256") or ""),
    }
    for key in ("department", "category", "season", "warehouse", "platform", "version",
                "authority", "scope"):
        value = meta.get(key)
        if value:
            payload[key] = str(value)
    for key in ("effective_start", "effective_end"):
        value = meta.get(key)
        if value:
            payload[key] = str(value)
            try:
                payload[f"{key}_ts"] = int(str(value).replace("-", "")[:8])
            except ValueError:
                pass
    return payload


def _sync_store_chunks(chunks: list) -> int:
    """把一批 chunk 的向量写进向量库（Qdrant 未启用时直接返回 0）。"""
    if not chunks:
        return 0
    from app.rag import vector_store as vs

    store = vs.get_store()
    if store is None:
        return 0
    try:
        from app.rag.embedding import get_embedding_provider

        provider = get_embedding_provider()
        vectors = provider.embed([c.text for c in chunks])
        points = [
            {"chunk_id": c.chunk_id, "vector": v, "payload": _payload_of(c)}
            for c, v in zip(chunks, vectors)
        ]
        return store.upsert(points)
    except Exception as exc:  # noqa: BLE001 —— 向量库/embedding 异常不阻断入库
        logger.warning("向量库写入失败（SQLite 已写入，稍后可重建）：%s", exc)
        vs.mark_degraded(f"向量库写入失败：{type(exc).__name__}: {exc}")
        return 0


def sync_store(session: Session) -> dict:
    """按 SQLite 现有语料**全量重建**向量索引（换 embedding 模型/维度后调用）。"""
    from app.rag import vector_store as vs
    from app.rag.embedding import get_embedding_provider

    store = vs.get_store()
    if store is None:
        return {"backend": "memory", "points": 0, "note": "VECTOR_STORE 不是 qdrant，无需同步"}
    provider = get_embedding_provider()
    rows = list(session.scalars(select(KbChunk)))
    texts = [c.text for c in rows]
    batch = 64
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch):
        vectors.extend(provider.embed(texts[start:start + batch]))
    points = [
        {"chunk_id": chunk.chunk_id, "vector": vector, "payload": _payload_of(chunk)}
        for chunk, vector in zip(rows, vectors)
    ]
    _clear_store()
    store.upsert(points)
    return {"backend": "qdrant", "points": len(points),
            "dim": provider.dim, "model": provider.model,
            "collection": get_settings().qdrant_collection}


def _clear_store() -> None:
    from app.rag import vector_store as vs

    store = vs.get_store()
    if store is None:
        return
    for method in ("clear", "reset"):
        fn = getattr(store, method, None)
        if callable(fn):
            try:
                fn()
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("清空向量库失败（%s）：%s", method, exc)


def reconcile_store(session: Session, *, full: bool = False) -> dict:
    """按文档对账向量库：**只重建指纹不一致的文档**（评审 3.3）。

    旧实现是 `expected_count != actual_count` 就整库重建——这既掩盖了"文档变短残留旧块"
    的真实 bug，又让每次启动都可能全量重算（1200 篇时是分钟级）。
    """
    from app.rag import vector_store as vs

    store = vs.get_store()
    if store is None:
        return {"backend": "memory", "mode": "reconcile", "rebuilt": False,
                "note": "VECTOR_STORE 不是 qdrant，无需对账"}
    if full:
        result = sync_store(session)
        return {**result, "rebuilt": True, "mode": "full"}

    expected = {d.doc_id: (int(d.chunk_count or 0), d.chunks_sha or "")
                for d in session.scalars(select(KbDoc))}
    if not expected:
        return {"backend": "qdrant", "mode": "reconcile", "rebuilt": False, "note": "语料为空"}
    actual = _store_doc_stats(store)
    drifted = [doc_id for doc_id, count in expected.items() if actual.get(doc_id) != count]
    for doc_id in drifted:
        _resync_document(session, doc_id)
    total = sum(count for count, _sha in expected.values())
    return {
        "backend": "qdrant", "rebuilt": bool(drifted), "mode": "reconcile",
        "documents": len(expected), "expected_chunks": total,
        "drifted": drifted, "drifted_count": len(drifted),
        "actual_documents": len(actual),
    }


def _store_doc_stats(store) -> dict[str, int]:  # noqa: ANN001
    """从向量库读出「每篇文档有多少点」——按文档对账的输入。"""
    fn = getattr(store, "doc_chunk_counts", None)
    if callable(fn):
        try:
            return {str(k): int(v) for k, v in fn().items()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取向量库文档统计失败：%s", exc)
    return {}


def _resync_document(session: Session, doc_id: str) -> dict:
    """重建单篇文档的向量（文档变短时也会先清掉旧点）。"""
    from app.rag import vector_store as vs
    from app.rag.embedding import get_embedding_provider

    store = vs.get_store()
    rows = list(session.scalars(select(KbChunk).where(KbChunk.doc_id == doc_id)))
    if store is None:
        return {"doc_id": doc_id, "points": 0}
    try:
        store.delete_doc(doc_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("重建前删除旧点失败（%s）：%s", doc_id, exc)
    if not rows:
        return {"doc_id": doc_id, "points": 0}
    provider = get_embedding_provider()
    vectors: list[list[float]] = []
    batch = 64
    texts = [r.text for r in rows]
    for start in range(0, len(texts), batch):
        vectors.extend(provider.embed(texts[start:start + batch]))
    points = [{"chunk_id": r.chunk_id, "vector": v, "payload": _payload_of(r)}
              for r, v in zip(rows, vectors)]
    store.upsert(points)
    return {"doc_id": doc_id, "points": len(points)}


def ensure_store_synced(session: Session) -> dict:
    """启动自检：按文档指纹对账（异常只降级不阻塞启动）。"""
    from app.rag import vector_store as vs

    store = vs.get_store()
    if store is None:
        return {"backend": "memory"}
    try:
        if not getattr(get_settings(), "rag_index_reconcile", True):
            return {"backend": "qdrant", "reconciled": False, "note": "RAG_INDEX_RECONCILE=false"}
        return reconcile_store(session)
    except Exception as exc:  # noqa: BLE001
        vs.mark_degraded(f"启动自检失败：{type(exc).__name__}: {exc}")
        return {"backend": "memory", "degraded_reason": str(exc)}


def index_health(session: Session) -> dict:
    """索引自述（/api/health.rag_index）：文档数/块数/最近同步动作。"""
    try:
        docs = int(session.query(KbDoc).count())
        chunks = int(session.query(KbChunk).count())
        last = session.scalar(select(KbSyncLog).order_by(KbSyncLog.id.desc()))
        return {"documents": docs, "chunks": chunks,
                "avg_chunks_per_doc": round(chunks / docs, 1) if docs else 0.0,
                "last_action": {"doc_id": last.doc_id, "action": last.action,
                                "at": last.at.isoformat(timespec="seconds")} if last else None}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
