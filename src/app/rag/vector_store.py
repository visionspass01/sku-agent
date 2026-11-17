"""向量存储抽象：进程内内存（默认降级） + Qdrant（可选生产路径）。

职责边界：
- **SQLite `kb_chunk` 是正文与元数据的唯一权威源**（`kb_doc`/`kb_chunk` 照旧）；
- Qdrant 只是"可重建的向量索引"：升级/换 embedding 模型 → 删集合重建（58 块 <1s）；
- 检索的融合（BM25 0.6/0.4）与启发式 Rerank 仍在 Python（语料小、可解释、可复现），
  Qdrant 只负责"向量候选 + payload 过滤"。
- 任何一步失败都**降级到内存检索**（哈希向量在 SQLite 里永远保留），演示不会 500。

Qdrant 两种模式（同一个客户端）：
- 服务器：`QDRANT_URL=http://qdrant:6333`；
- 本地无 Docker：`QDRANT_URL=local` 或 `local:<path>`（qdrant-client 官方本地模式，
  仅用于开发/测试，不用起容器也能跑通代码路径）。
"""

from __future__ import annotations

import datetime as dt
import logging
import pathlib
import uuid
from typing import Protocol

from app.config import DATA_DIR, get_settings

logger = logging.getLogger("sku-agent.vector_store")

NAMESPACE = uuid.NAMESPACE_URL


class VectorStoreError(RuntimeError):
    pass


def point_id(chunk_id: str) -> str:
    """Qdrant point id 必须是 UUID 或 uint64；chunk_id 形如 `sop-replenish#003`，用 uuid5 映射。"""
    return str(uuid.uuid5(NAMESPACE, chunk_id))


def collection_for(base: str, dim: int) -> str:
    """集合名带上维度：Qdrant 集合维度不可变，换 embedding 模型就换集合（旧集合可删）。

    这样换模型（0.6b 1024 → 4b 2560）不需要破坏性重建，也避免残留数据把新旧向量混在一起
    （qdrant-client 本地模式删集合后数据文件仍留在磁盘上，直接同名重建会读到旧数据）。
    """
    return f"{base}_{int(dim)}"


class VectorStore(Protocol):
    backend: str
    dim: int

    def search(self, vector: list[float], topk: int, filters: dict | None = None,
               as_of: dt.date | None = None) -> list[tuple[str, float]]: ...

    def upsert(self, points: list[dict]) -> int: ...

    def delete_doc(self, doc_id: str) -> int: ...

    def count(self) -> int: ...

    def doc_chunk_counts(self) -> dict: ...

    def clear(self) -> None: ...

    def health(self) -> dict: ...


# ---------------------------------------------------------------- Qdrant
class QdrantStore:
    backend = "qdrant"

    def __init__(self, url: str, collection: str, dim: int, timeout: float = 3.0) -> None:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        if not dim:
            raise VectorStoreError("维度未知：请先配置 embedding 模型（决定 Qdrant collection 维度）")
        self.url = url
        self.dim = int(dim)
        # 集合名带维度：换模型/换维度即换集合，不搞破坏性重建（见 collection_for）
        self.collection = collection_for(collection, self.dim)
        if url.startswith("local"):
            path = url.split(":", 1)[1].strip() if ":" in url else ""
            self.local_path = pathlib.Path(path) if path else (DATA_DIR / "qdrant_local")
            self.local_path.mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=str(self.local_path))
            self._mode = "local"
        else:
            self._client = QdrantClient(url=url, timeout=timeout)
            self._mode = "server"
        self._distance = Distance.COSINE
        self._vectors = VectorParams(size=self.dim, distance=self._distance)
        self._recreated = False
        self._ensure_collection()

    # ------------------------------------------------------------ 集合生命周期
    def _ensure_collection(self) -> None:
        from qdrant_client.models import Distance, VectorParams

        self._distance = Distance.COSINE
        self._vectors = VectorParams(size=self.dim, distance=self._distance)
        exists = self._collection_exists()
        if not exists:
            self._client.create_collection(
                collection_name=self.collection, vectors_config=self._vectors,
            )
            return
        info = self._client.get_collection(self.collection)
        size = ((info.config.params.vectors or {}) if info.config else {})
        current = getattr(size, "size", None) if not isinstance(size, dict) else size.get("size")
        if current and int(current) != self.dim:
            # 同名不同维：只可能是手工造出来的不一致集合，删掉重建（服务器模式可彻底清理）
            logger.warning("Qdrant 集合 %s 维度 %s != 配置维度 %s，重建集合",
                           self.collection, current, self.dim)
            self._client.delete_collection(self.collection)
            self._client.create_collection(
                collection_name=self.collection, vectors_config=self._vectors,
            )
            self._recreated = True

    def _collection_exists(self) -> bool:
        try:
            self._client.get_collection(self.collection)
            return True
        except Exception:  # noqa: BLE001 —— NotFound/网络异常都当作“需要创建/重建”
            return False

    # ------------------------------------------------------------ 检索
    def search(self, vector: list[float], topk: int, filters: dict | None = None,
               as_of: dt.date | None = None) -> list[tuple[str, float]]:
        from qdrant_client.models import FieldCondition, Filter, MatchAny, Range

        must: list = []
        filters = filters or {}
        for key in ("category", "season", "warehouse", "platform"):
            want = filters.get(key)
            if not want:
                continue
            allowed = want if isinstance(want, (list, tuple, set)) else [want]
            allowed = [a for a in allowed if a and a not in ("全部",)]
            if not allowed:
                continue
            must.append(FieldCondition(key=key, match=MatchAny(any=list(allowed))))
        department = filters.get("department")
        if department:
            allowed = department if isinstance(department, (list, tuple, set)) else [department]
            allowed = [a for a in allowed if a and a not in ("全部",)]
            # 与 `_passes_filter` 对齐：部门可见范围外的行不参与候选，但要带上「通用」
            if allowed:
                if "通用" not in allowed:
                    allowed = [*allowed, "通用"]
                must.append(FieldCondition(key="department", match=MatchAny(any=list(allowed))))
        if as_of is not None:
            # Qdrant 的 Range 只支持数值：payload 里存 yyyymmdd 整数（effective_*_ts）
            ts = int(as_of.strftime("%Y%m%d"))
            must.append(FieldCondition(key="effective_start_ts", range=Range(lte=ts)))
            must.append(FieldCondition(key="effective_end_ts", range=Range(gte=ts)))
        query_filter = Filter(must=must) if must else None
        result = self._client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=query_filter,
            limit=max(1, int(topk)),
            with_payload=True,
        )
        out: list[tuple[str, float]] = []
        for point in getattr(result, "points", []) or []:
            payload = point.payload or {}
            chunk_id = payload.get("chunk_id") or str(point.id)
            out.append((str(chunk_id), float(point.score or 0.0)))
        return out

    # ------------------------------------------------------------ 写入
    def upsert(self, points: list[dict]) -> int:
        from qdrant_client.models import PointStruct

        structs = [
            PointStruct(id=point_id(p["chunk_id"]), vector=p["vector"], payload=p.get("payload") or {})
            for p in points
        ]
        if not structs:
            return 0
        self._client.upsert(collection_name=self.collection, points=structs)
        return len(structs)

    def delete_doc(self, doc_id: str) -> int:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        self._client.delete(
            collection_name=self.collection,
            points_selector=Filter(must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]),
        )
        return 0

    def count(self) -> int:
        try:
            return int(self._client.count(self.collection).count)
        except Exception:  # noqa: BLE001
            return 0

    def doc_chunk_counts(self) -> dict[str, int]:
        """每篇文档在向量库里有多少点（按文档对账用，避免整库重建）。

        用 scroll 只取 payload 里的 doc_id：点数量在演示规模（千级）下开销可接受；
        没有这个方法时上游只能比较**总**点数，而"一篇变短 + 另一篇变长"总数不变，
        陈旧块就永远发现不了。
        """
        counts: dict[str, int] = {}
        offset = None
        for _ in range(200):        # 上限保护：单次对账不无限滚
            points, offset = self._client.scroll(
                collection_name=self.collection, limit=512, offset=offset,
                with_payload=["doc_id"], with_vectors=False,
            )
            for point in points:
                doc_id = str((point.payload or {}).get("doc_id") or "")
                if doc_id:
                    counts[doc_id] = counts.get(doc_id, 0) + 1
            if offset is None:
                break
        return counts

    def clear(self) -> None:
        """清空集合（换 embedding 模型后全量重建用）。"""
        from qdrant_client.models import Distance, VectorParams

        try:
            self._client.delete_collection(self.collection)
        except Exception:  # noqa: BLE001 —— 集合不存在时忽略
            pass
        self._client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
        )

    def health(self) -> dict:
        return {
            "backend": self.backend,
            "mode": self._mode,
            "url": self.url if self._mode == "server" else f"local:{getattr(self, 'local_path', '')}",
            "collection": self.collection,
            "dim": self.dim,
            "points": self.count(),
            "ok": True,
            "recreated": self._recreated,
            "note": "向量库只存向量与元数据（不存正文）；集合名带维度（换模型即换集合）；SQLite 仍是权威源",
        }

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 工厂
_store: VectorStore | None = None
_store_error: str = ""


def _dim_for_embedding() -> int:
    """向量维度以**实际生效的 embedding provider** 为准：哈希 512；腾讯 0.6b=1024 / 4b=2560。

    为什么不再直接按 `settings.embedding_backend` 取：配置为 tencent 但缺少 API Key 时，
    provider 会降级为哈希向量（512 维），而集合若按配置建成 1024 维，写入会因维度不匹配失败、
    检索整体降级为内存模式（Qdrant 容器白跑）。以 provider.dim 为准保证「集合维度 == 写入向量维度」。
    """
    from app.rag.vectorizer import DIM as HASH_DIM

    try:
        from app.rag.embedding import get_embedding_provider

        dim = int(getattr(get_embedding_provider(), "dim", 0) or 0)
        if dim > 0:
            return dim
    except Exception as exc:  # noqa: BLE001 —— provider 构建失败（缺依赖/配置异常）时按哈希维度
        logger.warning("embedding provider 不可用，向量维度按哈希向量处理：%s", exc)
    return HASH_DIM


def build_store() -> VectorStore | None:
    """按配置构建向量库；未启用/失败返回 None（调用方退回内存检索）。"""
    settings = get_settings()
    if (settings.vector_store or "memory").strip().lower() != "qdrant":
        return None
    url = (settings.qdrant_url or "").strip()
    if not url:
        global _store_error
        _store_error = "VECTOR_STORE=qdrant 但未配置 QDRANT_URL"
        logger.warning(_store_error)
        return None
    try:
        return QdrantStore(url=url, collection=settings.qdrant_collection,
                           dim=_dim_for_embedding(), timeout=settings.qdrant_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 —— 连接失败只降级，不阻塞启动
        _store_error = f"{type(exc).__name__}: {exc}"
        logger.warning("Qdrant 不可用，检索降级为内存模式：%s", _store_error)
        return None


def get_store() -> VectorStore | None:
    global _store
    if _store is None and not _store_error:
        _store = build_store()
    return _store


def mark_degraded(reason: str) -> None:
    global _store_error
    _store_error = reason
    logger.warning("向量检索降级为内存模式：%s", reason)


def store_status() -> dict:
    store = get_store()
    if store is None:
        return {"backend": "memory", "ok": True, "degraded": bool(_store_error),
                "degraded_reason": _store_error or "",
                "note": "进程内 numpy 余弦（哈希向量）；Qdrant 未启用或不可用"}
    status = store.health()
    # 即使 store 已建好，只要运行期出现过失败（写入/查询/启动自检）就算 degraded，
    # 让 health 能如实反映"当前检索其实退回了哈希余弦"。
    status["degraded"] = bool(_store_error)
    status["degraded_reason"] = _store_error or ""
    return status


def reset_store() -> None:
    global _store, _store_error
    if _store is not None:
        _store.close()
    _store = None
    _store_error = ""
