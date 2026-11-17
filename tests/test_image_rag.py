"""D10：本地知识库（`data/kb_image`）—— 语料解析、BM25+向量 RRF 召回、缓存、降级、Prompt 片段注入。"""

from __future__ import annotations

import pytest

from app.agents_image.nodes import plan_slots
from app.agents_image.rag import index as rag_index
from app.agents_image.rag.corpus import corpus_fingerprint, load_corpus
from app.agents_image.registry import get_registry, reset_registry
from app.agents_image.sources.fake_catalog import _PRODUCTS
from app.agents_image.state import ImageTaskState, RefImage, SlotPlan


@pytest.fixture(autouse=True)
def _fresh():
    rag_index.reset_index()
    reset_registry()
    yield
    rag_index.reset_index()
    reset_registry()


def test_corpus_loads_all_kinds():
    docs = load_corpus()
    assert len(docs) >= 8
    kinds = {doc.kind for doc in docs}
    assert {"platform", "slot", "fragment", "fix"} <= kinds
    ids = [doc.doc_id for doc in docs]
    assert len(ids) == len(set(ids))               # id 唯一
    assert all(doc.body and doc.title for doc in docs)


def test_corpus_missing_dir_is_empty_not_error(tmp_path):
    assert load_corpus(tmp_path / "nope") == []


def test_fingerprint_changes_with_content(tmp_path):
    base = tmp_path / "kb"
    base.mkdir()
    (base / "a.md").write_text("---\nid: a\nkind: fix\ntitle: A\n---\n正文一", encoding="utf-8")
    first = corpus_fingerprint(load_corpus(base))
    (base / "a.md").write_text("---\nid: a\nkind: fix\ntitle: A\n---\n正文二", encoding="utf-8")
    assert corpus_fingerprint(load_corpus(base)) != first


def test_retrieve_returns_relevant_platform_rule():
    hits = rag_index.retrieve("淘系 主图 白底 尺寸 文件大小", top_k=5)
    assert hits
    ids = [hit.doc.doc_id for hit in hits]
    assert "platform-taobao" in ids
    assert hits[0].score > 0


def test_retrieve_filters_by_kind():
    hits = rag_index.retrieve("白底不纯 灰边 修复", top_k=5, kind="fix")
    assert hits and all(hit.doc.kind == "fix" for hit in hits)


def test_retrieve_empty_query_and_empty_corpus():
    assert rag_index.retrieve("   ") == []
    empty = rag_index.ImageIndex([])
    assert empty.search("任意") == []


def test_cache_counts_hits():
    rag_index.retrieve("主图 白底 占比", top_k=3)
    before = rag_index.cache_stats()
    rag_index.retrieve("主图 白底 占比", top_k=3)
    after = rag_index.cache_stats()
    assert after["hits"] == before["hits"] + 1
    assert 0.0 <= after["hit_rate"] <= 1.0


def test_index_status_reports_corpus():
    status = rag_index.index_status()
    assert status["docs"] >= 8
    assert status["keyword_index"] in {True, False}
    assert status["fingerprint"]


def test_recall_fragments_groups_by_query():
    fragments = rag_index.recall_fragments(["主图 白底", "尺码对照 排版"], top_k=2)
    assert set(fragments) == {"主图 白底", "尺码对照 排版"}
    assert all(isinstance(items, list) for items in fragments.values())


def test_compose_injects_per_slot_fragments():
    state = ImageTaskState(prompt="白底主图", aspect="1:1", count=2,
                           refs=[RefImage(asset_id="a" * 8)],
                           attrs=dict(_PRODUCTS["P-1001"]["attrs"]))
    policy_slots = [
        {"slot": 1, "role": "主图", "type": "generate",
         "composition_id": "C-FULL-FRONT-TOP", "composition_label": "正面全身像·上装突出",
         "scene_style_id": "S-WHITE-E-COM", "scene_style_label": "白底电商风"},
        {"slot": 2, "role": "上装卖点", "type": "generate",
         "composition_id": "C-HALF-FRONT", "composition_label": "正面半身",
         "scene_style_id": "S-STUDIO-FORMAL", "scene_style_label": "影棚正式风"},
    ]
    state.slots = [SlotPlan(**item) for item in policy_slots]
    fragments = {
        1: ["主图：白底产品占 70%"],
        2: ["卖点图：可用微距突出面料"],
    }
    drafts = plan_slots.compose(state, get_registry(), fragments=fragments)
    assert "白底产品占 70%" in drafts[0].prompt
    assert "微距突出面料" in drafts[1].prompt
    assert drafts[0].fragments == ["主图：白底产品占 70%"]
    assert "白底产品占 70%" not in drafts[1].prompt     # 片段按图位隔离


def test_retrieve_is_stable_across_calls():
    first = [hit.doc.doc_id for hit in rag_index.retrieve("细节特写 微距", top_k=3)]
    second = [hit.doc.doc_id for hit in rag_index.retrieve("细节特写 微距", top_k=3)]
    assert first == second
