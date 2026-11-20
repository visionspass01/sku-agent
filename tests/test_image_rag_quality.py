"""第五轮 W5：检索质量与稳定性（P1-1 / P1-2）。

- **稳定哈希**（P1-2）：向量桶用 blake2b，跨进程/重启一致（过去 `hash()` 受 PYTHONHASHSEED 影响）；
- **查询改写**：业务同义词扩展（主图/首图、白底/纯白…）；
- **相似度阈值**：`min_score` 过滤弱向量命中（默认 0，标定后再开）；
- **kind 过滤**：`fixes/` 语料只给 Critic 失败用，不再混进写 Prompt 的召回；
- **启发式重排**：标题命中 > 词覆盖 > RRF 分。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.agents_image.rag import index as rag_index
from app.agents_image.rag.corpus import Doc
from app.agents_image.rag.synonyms import rewrite_query

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_index():
    rag_index.reset_index()
    yield
    rag_index.reset_index()


# ---------------------------------------------------------------- P1-2 稳定哈希
def test_vector_buckets_are_stable_across_processes():
    """同一段文本在两个新进程里必须得到同一个向量（hash() 随机化会破坏这一点）。"""
    script = (
        "import sys; sys.path.insert(0, %r);"
        "from app.agents_image.rag.index import _vector;"
        "print(sum((i + 1) * v for i, v in enumerate(_vector('白底主图 米色 针织开衫'))))"
    ) % str(ROOT / "src")
    outputs = []
    for _ in range(2):
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, encoding="utf-8")
        assert proc.returncode == 0, proc.stderr
        outputs.append(proc.stdout.strip())
    assert outputs[0] == outputs[1], outputs
    assert outputs[0] != "0.0"


def test_same_text_gives_same_vector_in_process():
    assert rag_index._vector("主图 白底 留白") == rag_index._vector("主图 白底 留白")


# ---------------------------------------------------------------- P1-1 查询改写
def test_rewrite_query_expands_business_synonyms():
    rewritten = rewrite_query("白底主图 留白")
    assert "首图" in rewritten or "封面图" in rewritten
    assert "纯白" in rewritten or "白背景" in rewritten
    assert "边距" in rewritten or "margin" in rewritten


def test_rewrite_query_keeps_short_query_untouched():
    assert rewrite_query("任意无关内容") == "任意无关内容"
    assert rewrite_query("") == ""


# ---------------------------------------------------------------- kind / 阈值 / 重排
def _index(docs: list[Doc]) -> rag_index.ImageIndex:
    return rag_index.ImageIndex(docs)


def _doc(doc_id: str, kind: str, title: str, body: str, tags: list[str] | None = None) -> Doc:
    return Doc(doc_id=doc_id, kind=kind, title=title, body=body, tags=tags or [])


def test_kind_filter_only_returns_requested_kind():
    index = _index([
        _doc("slot-main", "slot", "图位 1 主图", "白底 主图 正面全身 占比 留白"),
        _doc("fix-identity", "fix", "修复：身份漂移", "颜色 版型 logo 修复 白底"),
        _doc("platform-taobao", "platform", "淘系图片规范", "白底 比例 文件大小"),
    ])
    fixes = index.search("白底 修复", top_k=5, kind="fix")
    assert [hit.doc.kind for hit in fixes] == ["fix"]

    slots = index.search("白底 主图", top_k=5, kinds=("slot", "platform"))
    assert slots, slots
    assert all(hit.doc.kind in {"slot", "platform"} for hit in slots)


def test_min_score_filters_weak_vector_hits():
    index = _index([
        _doc("slot-main", "slot", "图位 1 主图", "白底 主图 正面全身 占比 留白"),
        _doc("fragment-light", "fragment", "光影片段", "柔光 侧逆光 背景干净"),
    ])
    # 查询只与"主图"那片强相关：给一个很高的阈值后，弱命中应该被过滤掉
    hits = index.search("完全无关的词 zzz", top_k=5, min_score=0.99)
    assert hits == []
    hits = index.search("主图 白底", top_k=5, min_score=0.0)
    assert hits


def test_heuristic_rerank_prefers_title_hits():
    index = _index([
        _doc("a", "fragment", "通用光影说明", "白底 图位 主图 场景 留白 占比 全部提到一遍"),
        _doc("b", "slot", "图位 1 主图", "白底 主图 正面全身"),
    ])
    hits = index.search("图位 1 主图", top_k=2)
    assert hits[0].doc.doc_id == "b", [hit.as_dict() for hit in hits]
    assert hits[0].rerank_score >= hits[1].rerank_score


def test_rerank_can_be_disabled():
    index = _index([
        _doc("a", "fragment", "通用光影说明", "白底 图位 主图 场景 留白 占比"),
        _doc("b", "slot", "图位 1 主图", "白底 主图"),
    ])
    with_rerank = index.search("图位 1 主图", top_k=2, rerank_backend="heuristic")
    without = index.search("图位 1 主图", top_k=2, rerank_backend="none")
    assert with_rerank[0].doc.doc_id == "b"
    assert without[0].rerank_score == 0.0


# ---------------------------------------------------------------- 真实语料
def test_real_corpus_retrieval_excludes_fix_docs_by_default_kinds():
    hits = rag_index.retrieve("白底主图 比例", top_k=3,
                              kinds=("slot", "fragment", "platform"))
    assert hits, "真实语料应能召回到写法片段"
    assert all(hit.doc.kind in {"slot", "fragment", "platform"} for hit in hits)


def test_real_corpus_fix_retrieval_works():
    hits = rag_index.retrieve("修复 身份 颜色 版型", top_k=1, kind="fix")
    assert hits and hits[0].doc.kind == "fix"
    assert "修复" in hits[0].snippet or "修法" in hits[0].snippet
