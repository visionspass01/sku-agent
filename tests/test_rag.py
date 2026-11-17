"""RAG 链路测试：分块 / 查询改写 / 混合召回 / 元数据过滤 / 增量更新。"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest

from app.config import KB_DIR
from app.db.engine import session_scope
from app.rag.chunker import chunk_document
from app.rag.hybrid import get_retriever, rewrite_query
from app.rag.index import build_index
from app.rag.loader import load_documents


class TestChunker:
    def test_chunks_have_metadata_and_ids(self):
        docs = {d.doc_id: d for d in load_documents(KB_DIR)}
        doc = docs["sop-replenish"]
        chunks = chunk_document(doc)
        assert chunks
        assert len({c.chunk_id for c in chunks}) == len(chunks)
        assert all(c.chunk_id.startswith("sop-replenish#") for c in chunks)
        assert all(c.meta["doc_id"] == "sop-replenish" for c in chunks)

    def test_table_rows_are_separate_chunks(self):
        docs = {d.doc_id: d for d in load_documents(KB_DIR)}
        chunks = chunk_document(docs["sop-supplier"])
        table_chunks = [c for c in chunks if c.text.count("|") >= 1]
        assert any("SUP-01" in c.text for c in table_chunks)

    def test_key_facts_are_scoped_to_sections(self):
        """事实按小节下发：不相关的小节不该带着口径（复审 P2-2）。"""
        docs = {d.doc_id: d for d in load_documents(KB_DIR)}
        chunks = chunk_document(docs["sop-clearance"])
        by_section = {c.meta["section"]: [f["key"] for f in c.meta["key_facts"]] for c in chunks}
        assert by_section["触发条件"] == ["clearance_trigger_days"]
        assert by_section["折扣阶梯"] == ["clearance_min_discount"]
        # 其余小节（数量与节奏/审批/风险提示）不应携带任何口径事实
        for section, keys in by_section.items():
            if section not in ("触发条件", "折扣阶梯"):
                assert keys == [], (section, keys)

    def test_doc_level_facts_apply_everywhere(self):
        docs = {d.doc_id: d for d in load_documents(KB_DIR)}
        chunks = chunk_document(docs["sop-lifecycle"])
        assert chunks
        assert all(c.meta["key_facts"] for c in chunks), "全文档事实应出现在每个块上"

    def test_facts_for_matches_section_by_substring(self):
        docs = {d.doc_id: d for d in load_documents(KB_DIR)}
        women = docs["sop-clearance-women"]
        # 声明「触发条件」，块标题是「触发条件（女装）」→ 双向子串匹配命中
        assert women.facts_for("触发条件（女装）")
        assert women.facts_for("执行节奏（女装）") == []
        assert women.facts_for(None) == []
        assert docs["sop-lifecycle"].facts_for("任意小节"), "* 事实应恒命中"

    def test_fact_coverage_reports_department_gaps(self):
        """哪个部门"一篇都没声明口径"要能看见（复审 P3-2：这是部门例外缺一块的根因）。"""
        from app.rag.loader import kb_fact_coverage, reset_coverage_cache

        reset_coverage_cache()
        coverage = kb_fact_coverage()
        assert "department_gaps" in coverage
        # 演示语料里男装两篇都没有 key_facts → 男装是缺口部门
        assert "男装" in coverage["department_gaps"]
        assert all(doc_id.startswith("sop-") for doc_id in coverage["department_gaps"]["男装"])



class TestRewrite:
    def test_synonyms(self):
        q = rewrite_query("断码 怎么处理")
        assert "缺码" in q or "尺码断货" in q
        assert "清仓" in rewrite_query("清仓折扣")


class TestRetrieval:
    def test_relevant_doc_ranked_first(self, seeded):
        with session_scope() as session:
            retriever = get_retriever(session)
            hits = retriever.search("断码补全 尺码 调拨", topk=5)
            assert hits
            top_doc = hits[0].doc_id
            assert top_doc in ("sop-sizegap", "sop-replenish")

    def test_clearance_query(self, seeded):
        with session_scope() as session:
            retriever = get_retriever(session)
            hits = retriever.search("清仓 折扣 6 折", topk=3)
            assert hits[0].doc_id == "sop-clearance"

    def test_effective_date_filter(self, seeded):
        with session_scope() as session:
            retriever = get_retriever(session)
            hits = retriever.search("补货", topk=5, as_of=dt.date(2024, 6, 1))
            assert hits == []  # 文档生效期 2025/2026 年起

    def test_metadata_filter(self, seeded):
        with session_scope() as session:
            retriever = get_retriever(session)
            hits = retriever.search("清仓", topk=5, filters={"season": "春夏"})
            assert all(h.meta.get("season") in ("全部", "春夏") for h in hits)

    def test_department_filter(self, seeded):
        """部门用户：只能检索「本部门 + 通用」SOP。"""
        with session_scope() as session:
            retriever = get_retriever(session)
            men_hits = retriever.search(
                "女装 连衣裙 清仓 折扣", topk=5, filters={"department": ["男装", "通用"]}
            )
            assert men_hits
            assert all(h.meta["department"] in ("男装", "通用") for h in men_hits)

            women_hits = retriever.search(
                "女装 连衣裙 清仓 折扣", topk=5, filters={"department": ["女装", "通用"]}
            )
            assert any(h.meta["department"] == "女装" for h in women_hits)


class TestIncremental:
    @pytest.fixture(autouse=True)
    def _restore_kb_index(self):
        """这些用例会拿临时目录重建索引（等价于"整库换成另一份语料"）。

        跑完必须把共享测试库的索引恢复成仓库里的真实知识库，否则后续用例会检索到
        一份残缺语料——这类"跨用例污染"当年把 RAG 评测结果搞得忽高忽低。
        """
        yield
        with session_scope() as session:
            build_index(session)

    def test_only_changed_doc_reindexed(self, seeded, tmp_path: Path):
        kb_src = KB_DIR
        kb_dst = tmp_path / "kb"
        shutil.copytree(kb_src, kb_dst)

        with session_scope() as session:
            first = build_index(session, kb_dst)
        assert first.chunks >= 8

        # 修改一篇文档
        target = kb_dst / "sop-clearance.md"
        target.write_text(
            target.read_text(encoding="utf-8").replace("6 折", "5.8 折（演示修改）"),
            encoding="utf-8",
        )
        with session_scope() as session:
            second = build_index(session, kb_dst)
        assert "sop-clearance" in second.updated
        assert "sop-replenish" in second.skipped

        # 检索能命中修改后的内容
        with session_scope() as session:
            retriever = get_retriever(session)
            hits = retriever.search("清仓折扣下限", topk=5)
            assert any("5.8 折" in h.text for h in hits)
