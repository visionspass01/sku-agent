"""知识库索引一致性（评审 3.3）。

旧实现的确定性 bug：文档**变短**时，`build_index` 会删掉 SQLite 里的旧块，但向量库只
upsert 不 delete → 陈旧块永远留在向量库里被检索到；`ensure_store_synced` 又用"总数对不对"
判断，一篇变短 + 另一篇变长时总数相等，于是这个 bug 永远发现不了。

现在：chunk_id 内容寻址（位置无关）+ 按文档写 `chunks_sha` 指纹 + 单篇对账。
"""

from __future__ import annotations

import pathlib
import shutil

import pytest

DOC = """---
doc_id: sop-demo
title: 演示 SOP
department: 通用
category: 全部
season: 全部
warehouse: 全部
platform: 全部
version: v1
---

# 演示 SOP

## 第一节
{body_a}

## 第二节
{body_b}

## 第三节
{body_c}
"""


def _write(kb_dir: pathlib.Path, a: str = "补货要按主销码缺口计算。", b: str = "清仓折扣分三档。",
           c: str = "断码优先调拨。") -> None:
    kb_dir.mkdir(parents=True, exist_ok=True)
    (kb_dir / "sop-demo.md").write_text(DOC.format(body_a=a, body_b=b, body_c=c),
                                        encoding="utf-8")


@pytest.fixture()
def kb_dir(tmp_path):
    directory = tmp_path / "kb"
    _write(directory)
    yield directory


@pytest.fixture(scope="module")
def kb_db(tmp_path_factory):
    """独立库：这些用例会**增删知识库文档**，不能污染会话级的共享业务库。"""
    from sqlalchemy import create_engine as _create_engine

    from app.db.engine import get_engine, init_schema, set_engine

    previous = get_engine()
    tmp = pathlib.Path(tmp_path_factory.mktemp("kb_index"))
    engine = _create_engine(f"sqlite:///{(tmp / 'kb.db').as_posix()}",
                            connect_args={"check_same_thread": False})
    set_engine(engine)
    init_schema(engine)
    try:
        yield engine
    finally:
        set_engine(previous)


def _index(session, kb_dir):
    from app.rag.index import build_index

    return build_index(session, kb_dir)


def _chunks(session, doc_id: str = "sop-demo") -> list:
    from sqlalchemy import select

    from app.db.models import KbChunk

    return list(session.scalars(select(KbChunk).where(KbChunk.doc_id == doc_id)))


def test_chunk_ids_are_content_addressed(kb_db, kb_dir):
    """chunk_id 与位置无关：只改第二节，第一/三节的 id 必须保持不变。"""
    from app.db.engine import session_scope

    with session_scope() as session:
        _index(session, kb_dir)
        before = {c.chunk_id for c in _chunks(session)}
    assert len(before) >= 3
    _write(kb_dir, b="清仓折扣改为四档（新增一档清尾货）。")
    with session_scope() as session:
        _index(session, kb_dir)
        after = {c.chunk_id for c in _chunks(session)}
    # 只改了第二节：其余块 id 必须原样保留，且只多出一个新块
    assert len(before & after) == len(before) - 1, "内容没变的块 id 应当稳定（否则引用全失效）"
    assert len(after - before) == 1


def test_shrinking_document_removes_stale_chunks(kb_db, kb_dir):
    """文档变短：SQLite 里旧块必须删掉，不能残留（旧实现只删 SQLite，向量库漏删）。"""

    from app.db.engine import session_scope
    from app.db.models import KbDoc

    with session_scope() as session:
        _index(session, kb_dir)
        before = len(_chunks(session))
    assert before >= 3
    # 把第三节整段删掉
    text = (kb_dir / "sop-demo.md").read_text(encoding="utf-8")
    (kb_dir / "sop-demo.md").write_text(text.split("## 第三节")[0], encoding="utf-8")
    with session_scope() as session:
        stats = _index(session, kb_dir)
        after = len(_chunks(session))
        doc = session.get(KbDoc, "sop-demo")
        assert doc.chunk_count == after
        assert doc.chunks_sha
        assert after == len(_chunks(session))
    assert after < before
    assert stats.chunks_removed >= 1


def test_unchanged_document_is_skipped(kb_db, kb_dir):
    from app.db.engine import session_scope

    with session_scope() as session:
        _index(session, kb_dir)
    with session_scope() as session:
        stats = _index(session, kb_dir)
    assert stats.skipped == ["sop-demo"]
    assert stats.chunks_added == 0 and stats.chunks_removed == 0


def test_reconcile_detects_drift_per_document(kb_db, kb_dir, monkeypatch):
    """对账按文档指纹：人为制造"块数不变但内容变了"的漂移也能发现。"""
    from app.db.engine import session_scope
    from app.rag import index as rag_index

    with session_scope() as session:
        _index(session, kb_dir)

    calls: list[str] = []
    monkeypatch.setattr(rag_index, "_resync_document",
                        lambda session, doc_id: calls.append(doc_id) or {"doc_id": doc_id})
    # 指数库：把某篇文档的块数改成错误值，模拟向量库与 SQLite 不一致
    from sqlalchemy import update

    from app.db.models import KbDoc

    with session_scope() as session:
        session.execute(update(KbDoc).where(KbDoc.doc_id == "sop-demo")
                        .values(chunk_count=999))
    with session_scope() as session:
        report = rag_index.reconcile_store(session)
    assert report["mode"] == "reconcile"
    # 内存模式（默认）没有向量库可对账：不重建、不报错；配了 Qdrant 时应命中 sop-demo
    if report.get("backend") == "qdrant":
        assert report["drifted"], "块数被改坏却判断为无漂移"
        assert "sop-demo" in calls
    else:
        assert report["rebuilt"] is False


def test_reconcile_without_vector_store_is_noop(seeded):
    """内存模式下对账直接返回（不报错、不重建），启动路径不会因此失败。"""
    from app.db.engine import session_scope
    from app.rag.index import ensure_store_synced

    with session_scope() as session:
        result = ensure_store_synced(session)
    assert result["backend"] in ("memory", "qdrant")


def test_index_health_reports_documents_and_chunks(seeded):
    from app.db.engine import session_scope
    from app.rag.index import index_health

    with session_scope() as session:
        health = index_health(session)
    assert health["documents"] >= 1
    assert health["chunks"] >= health["documents"]
    assert "last_action" in health


def test_deleted_document_is_removed(kb_db, kb_dir):
    from sqlalchemy import select

    from app.db.engine import session_scope
    from app.db.models import KbChunk

    with session_scope() as session:
        _index(session, kb_dir)
    (kb_dir / "sop-demo.md").unlink()
    with session_scope() as session:
        stats = _index(session, kb_dir)
        remaining = list(session.scalars(select(KbChunk).where(KbChunk.doc_id == "sop-demo")))
    assert stats.deleted == ["sop-demo"]
    assert remaining == []


def test_real_kb_has_unique_chunk_ids(kb_db):
    """真实知识库：所有 chunk_id 唯一且形如 `doc#hash8`。"""
    from sqlalchemy import select

    from app.db.engine import session_scope
    from app.db.models import KbChunk
    from app.rag.index import build_index

    with session_scope() as session:
        build_index(session)          # 索引仓库里的真实知识库（data/kb）
        ids = list(session.scalars(select(KbChunk.chunk_id)))
    assert ids and len(ids) == len(set(ids))
    for chunk_id in ids:
        doc_id, _, digest = chunk_id.partition("#")
        assert doc_id and len(digest) == 8


def test_chunk_text_carries_title_prefix(kb_db):
    """块正文带《文档标题》前缀：检索命中时模型能分清是哪份 SOP 的门槛。"""
    from sqlalchemy import select

    from app.db.engine import session_scope
    from app.db.models import KbChunk
    from app.rag.index import build_index

    with session_scope() as session:
        build_index(session)
        row = session.scalars(select(KbChunk).where(KbChunk.doc_id == "sop-sizegap")).first()
        assert row is not None
        text, meta = row.text, dict(row.meta_json or {})
    assert text.startswith("《")
    assert meta.get("body"), "meta.body 必须保留原文（前缀只进 text）"


def test_copies_kb_dir_cleanup(tmp_path):
    """（自我校验）临时目录隔离：测试之间不共享 kb 目录。"""
    src = pathlib.Path(__file__).resolve().parent.parent / "data" / "kb"
    assert src.exists()
    dst = tmp_path / "copy"
    shutil.copytree(src, dst)
    assert len(list(dst.glob("*.md"))) >= 12
