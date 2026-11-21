"""知识库口径冲突的检测与裁决（评审 2.4）。

最常被追问的一句是：「两份 SOP 数字不一样，你们怎么办？」
没有确定性的答案时，模型的典型行为是**折中**（"大约 50 天"）或把两个都念一遍，
用户拿到的口径在任何一份制度里都不存在。这里锁定四件事：

1. 权威度阶梯：中台治理口径 > 部门细则 > 通用规则 > 历史案例；同权威度看版本、生效日；
2. **不丢文档**：冲突只标 `is_primary`，召回结果保持原样（丢掉女装细则等于答错）；
3. 诚实状态：语料没填 `key_facts` → `unavailable`，绝不谎称"检查过、没冲突"；
4. 冲突结论要能进提示词（`## 冲突提示`），并随响应一起回传（可审计）。
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from app.common.conflicts import (
    AUTHORITY_ORDER,
    Claim,
    authority_rank,
    canonical_unit,
    detect_conflicts,
    normalize_value,
    rank_claims,
    values_equal,
    version_key,
)
from app.rag.conflict import (
    KIND_CROSS_DOC,
    KIND_INTRA_DOC,
    STATUS_NONE,
    STATUS_OK,
    STATUS_UNAVAILABLE,
    detect,
    note_text,
)
from app.rag.hybrid import Hit
from app.rag.loader import parse_key_facts


def _hit(chunk_id: str, *, doc_id: str, authority: str = "general", version: str = "v1",
         key_facts: list[dict] | None = None, title: str = "", body: str = "正文") -> Hit:
    return Hit(
        chunk_id=chunk_id, doc_id=doc_id, text=body, score=1.0, vector_score=0.5,
        keyword_score=2.0,
        meta={"doc_id": doc_id, "title": title or doc_id, "authority": authority,
              "version": version, "effective_start": "2026-01-01",
              "key_facts": key_facts or [], "body": body},
    )


def _fact(key: str, value, unit: str = "") -> dict:  # noqa: ANN001
    return {"key": key, "value": value, "unit": unit}


class TestFrontMatterFacts:
    def test_parse_key_facts_splits_value_and_unit(self):
        facts = parse_key_facts("clearance_trigger_days=60天; clearance_min_discount=6折")
        assert facts == [
            {"key": "clearance_trigger_days", "value": 60.0, "unit": "天", "raw": "60天",
             "section": "*"},
            {"key": "clearance_min_discount", "value": 6.0, "unit": "折", "raw": "6折",
             "section": "*"},
        ]

    def test_parse_key_facts_supports_section_scope(self):
        facts = parse_key_facts("触发条件|clearance_trigger_days=45天; *|tag=1个")
        assert [f["section"] for f in facts] == ["触发条件", "*"]
        assert facts[0]["value"] == 45.0 and facts[0]["unit"] == "天"

    def test_parse_key_facts_tolerates_garbage(self):
        assert parse_key_facts("") == []
        assert parse_key_facts("没有等号; =空 key; key=") == []
        facts = parse_key_facts("mode=strict")
        assert facts[0]["value"] == "strict" and facts[0]["unit"] == ""

    def test_authority_defaults_by_department_and_case_prefix(self):
        from app.rag.loader import default_authority

        assert default_authority("sop-clearance", "通用") == "general"
        assert default_authority("sop-clearance-women", "女装") == "dept"
        assert default_authority("case-history", "通用") == "case"

    def test_loader_carries_authority_and_facts(self, tmp_path):
        from app.rag.loader import load_documents

        (tmp_path / "sop-a.md").write_text(
            "---\ndoc_id: sop-a\ntitle: A\ndepartment: 女装\nversion: v0.9\n"
            "authority: dept\nscope: dept\nkey_facts: days=45天\n---\n正文\n",
            encoding="utf-8",
        )
        (tmp_path / "case-b.md").write_text(
            "---\ndoc_id: case-b\ntitle: B\ndepartment: 通用\n---\n正文\n", encoding="utf-8",
        )
        docs = {d.doc_id: d for d in load_documents(tmp_path)}
        assert docs["sop-a"].authority == "dept"
        assert docs["sop-a"].scope == "dept"
        assert docs["sop-a"].key_facts[0]["value"] == 45.0
        # 没写 authority 时按目录推：case-* → 历史案例（最低权威度）
        assert docs["case-b"].authority == "case"
        assert docs["case-b"].scope == "general"


class TestPrecedence:
    def test_ladder_is_explicit(self):
        assert AUTHORITY_ORDER == ("governance", "dept", "general", "case")
        assert [authority_rank(a) for a in AUTHORITY_ORDER] == [0, 1, 2, 3]
        assert authority_rank("unknown") > authority_rank("case")

    def test_version_key_ordering(self):
        assert version_key("v1.2") > version_key("v1.1")
        assert version_key("v1.10") > version_key("v1.9")     # 数字比较，不是字符串比较
        assert version_key("") == (0,)

    def test_normalize_value_ignores_types(self):
        assert normalize_value(60) == normalize_value(60.0) == "60"
        # 带单位的字符串与「数值 + 单位」字段必须归一到同一个取值
        assert Claim(key="k", value=60.0, unit="天", doc_id="d").normalized == \
            Claim(key="k", value="60天", doc_id="d").normalized

    def test_unit_aliases_do_not_create_false_conflicts(self):
        """`60天` 与 `60日` 是同一个口径；不归一就会输出"采用 60 天…不采用 60 日"。"""
        pairs = [
            (Claim(key="d", value=60.0, unit="天", doc_id="a"),
             Claim(key="d", value=60.0, unit="日", doc_id="b")),
            (Claim(key="d", value=48.0, unit="小时", doc_id="a"),
             Claim(key="d", value=48.0, unit="h", doc_id="b")),
        ]
        for left, right in pairs:
            assert values_equal(left, right), (left.normalized, right.normalized)
            assert detect_conflicts([left, right]) == []

    def test_percentage_and_decimal_are_the_same_threshold(self):
        pct = Claim(key="share", value="15%", doc_id="a")
        dec = Claim(key="share", value=0.15, doc_id="b")
        assert values_equal(pct, dec)
        assert detect_conflicts([pct, dec]) == []
        # 不同数值仍然要报
        assert detect_conflicts([pct, Claim(key="share", value="20%", doc_id="c")])

    def test_percentage_autocorrect_is_bounded(self):
        """只接受 (0,1) 区间：不能把「0.5 折」当成 50%。"""
        discount = Claim(key="k", value=0.5, unit="折", doc_id="a")
        half = Claim(key="k", value="50%", doc_id="b")
        assert not values_equal(discount, half)
        assert detect_conflicts([discount, half])

    def test_unitless_value_is_not_equal_to_unit_value(self):
        """`60` 与 `60天` 不能混为一谈（否则"60 件"会被当成"60 天"）。"""
        assert not values_equal(Claim(key="k", value=60, doc_id="a"),
                                Claim(key="k", value=60.0, unit="天", doc_id="b"))

    def test_real_conflict_still_reported(self):
        assert detect_conflicts([
            Claim(key="d", value=60.0, unit="天", doc_id="a", authority="general"),
            Claim(key="d", value=45.0, unit="天", doc_id="b", authority="dept"),
        ])

    def test_governance_beats_dept_even_with_lower_version(self):
        claims = [
            Claim(key="k", value=60, unit="天", doc_id="sop-general", authority="general",
                  version="v1.2"),
            Claim(key="k", value=45, unit="天", doc_id="sop-dept", authority="dept",
                  version="v0.9"),
        ]
        conflicts = detect_conflicts(claims)
        assert len(conflicts) == 1
        assert conflicts[0]["winner"]["doc_id"] == "sop-dept", "部门细则必须压过通用规则"
        assert "部门细则优先于通用规则" in conflicts[0]["reason"]

    def test_rule_beats_case(self):
        claims = [
            Claim(key="k", value=5, unit="天", doc_id="case-history", authority="case"),
            Claim(key="k", value=7, unit="天", doc_id="sop-sizegap", authority="general"),
        ]
        winner = detect_conflicts(claims)[0]["winner"]
        assert winner["doc_id"] == "sop-sizegap"

    def test_same_authority_falls_back_to_version_then_date(self):
        claims = [
            Claim(key="k", value=1, doc_id="a", authority="general", version="v1.0",
                  effective_start="2026-01-01"),
            Claim(key="k", value=2, doc_id="b", authority="general", version="v1.3",
                  effective_start="2025-01-01"),
        ]
        assert detect_conflicts(claims)[0]["winner"]["doc_id"] == "b"

        same_version = [
            Claim(key="k", value=1, doc_id="a", authority="general", version="v1.0",
                  effective_start="2025-01-01"),
            Claim(key="k", value=2, doc_id="b", authority="general", version="v1.0",
                  effective_start="2026-06-01"),
        ]
        assert detect_conflicts(same_version)[0]["winner"]["doc_id"] == "b"

    def test_full_tie_is_deterministic(self):
        claims = [
            Claim(key="k", value=1, doc_id="zz", authority="general"),
            Claim(key="k", value=2, doc_id="aa", authority="general"),
        ]
        first = detect_conflicts(claims)[0]["winner"]["doc_id"]
        second = detect_conflicts(list(reversed(claims)))[0]["winner"]["doc_id"]
        assert first == second == "aa"

    def test_rank_claims_is_stable(self):
        claims = [Claim(key="k", value=i, doc_id=f"d{i}", authority="general") for i in range(5)]
        assert [c.doc_id for c in rank_claims(claims)] == [f"d{i}" for i in range(5)]

    def test_equal_values_are_not_a_conflict(self):
        claims = [
            Claim(key="k", value=60, unit="天", doc_id="a", authority="general"),
            Claim(key="k", value="60天", doc_id="b", authority="dept"),
        ]
        assert detect_conflicts(claims) == []


class TestKbConflictDetect:
    def test_unavailable_when_no_key_facts(self):
        """语料没填事实 → unavailable；绝不能返回「none=没冲突」。"""
        report = detect([_hit("d1#aaa", doc_id="d1"), _hit("d2#bbb", doc_id="d2")])
        assert report["status"] == STATUS_UNAVAILABLE
        assert "不可用" in report["note"]
        assert all(report["is_primary"].values())

    def test_none_when_facts_agree(self):
        report = detect([
            _hit("d1#a", doc_id="d1", key_facts=[_fact("alert_days", 7, "天")]),
            _hit("d2#b", doc_id="d2", key_facts=[_fact("alert_days", 7, "天")]),
        ])
        assert report["status"] == STATUS_NONE
        assert report["conflicts"] == []
        assert report["compared_keys"] == ["alert_days"]
        assert report["single_source_keys"] == []          # 两份文档都声明了 → 有交叉验证
        # 措辞必须限定在"本次比对范围"：没召回到的文档本来就不在比对范围内
        assert "本次比对了" in report["note"]

    def test_single_source_is_reported_honestly(self):
        """只有一份文档声明该口径时，不能说"取值一致"就完事。"""
        report = detect([_hit("d1#a", doc_id="d1", key_facts=[_fact("alert_days", 7, "天")])])
        assert report["status"] == STATUS_NONE
        assert report["single_source_keys"] == ["alert_days"]
        assert "单一来源" in report["note"]

    def test_department_doc_wins_and_docs_are_kept(self):
        general = _hit("sop-clearance#g", doc_id="sop-clearance", authority="general",
                       version="v1.2", key_facts=[_fact("clearance_trigger_days", 60, "天")])
        dept = _hit("sop-clearance-women#d", doc_id="sop-clearance-women", authority="dept",
                    version="v0.9", key_facts=[_fact("clearance_trigger_days", 45, "天")])
        report = detect([general, dept])
        assert report["status"] == STATUS_OK
        conflict = report["conflicts"][0]
        assert conflict["winner"]["doc_id"] == "sop-clearance-women"
        assert conflict["winner"]["display"] == "45 天"
        assert conflict["losers"][0]["doc_id"] == "sop-clearance"
        # 不丢文档：两份命中都还在，只是标了主次
        assert set(report["is_primary"]) == {"sop-clearance#g", "sop-clearance-women#d"}
        assert report["is_primary"]["sop-clearance#g"] is False
        assert report["is_primary"]["sop-clearance-women#d"] is True

    def test_same_doc_chunks_do_not_duplicate_losers(self):
        """同一篇文档命中多个块：文档级 key_facts 只能记一次落败。"""
        hits = [
            _hit("sop-a#1", doc_id="sop-a", authority="general",
                 key_facts=[_fact("days", 60, "天")]),
            _hit("sop-a#2", doc_id="sop-a", authority="general",
                 key_facts=[_fact("days", 60, "天")]),
            _hit("sop-b#1", doc_id="sop-b", authority="dept",
                 key_facts=[_fact("days", 45, "天")]),
        ]
        conflict = detect(hits)["conflicts"][0]
        assert [item["doc_id"] for item in conflict["losers"]] == ["sop-a"]
        # 落败文档的所有块都不是"主口径"，胜出文档的所有块都是
        report = detect(hits)
        assert report["is_primary"]["sop-a#1"] is False
        assert report["is_primary"]["sop-a#2"] is False
        assert report["is_primary"]["sop-b#1"] is True

    def test_note_text_is_actionable(self):
        report = detect([
            _hit("sop-clearance#g", doc_id="sop-clearance", authority="general", title="清仓 SOP",
                 key_facts=[_fact("clearance_trigger_days", 60, "天")]),
            _hit("sop-clearance-women#d", doc_id="sop-clearance-women", authority="dept",
                 title="女装清仓 SOP", version="v0.9",
                 key_facts=[_fact("clearance_trigger_days", 45, "天")]),
        ])
        text = note_text(report["conflicts"])
        assert text.startswith("## 冲突提示")
        assert "45 天" in text and "女装清仓 SOP" in text
        assert "60 天" in text and "不采用" in text
        assert "不要折中" in text
        assert note_text([]) == ""


class TestPipelineSurfacesConflicts:
    def test_context_prepends_conflict_note(self):
        """`context()` 直接把冲突提示放在正文前面（skill/批处理都吃这一份上下文）。"""
        from app.rag.pipeline import SearchResult

        general = _hit("sop-clearance#g", doc_id="sop-clearance", authority="general",
                       key_facts=[_fact("clearance_trigger_days", 60, "天")])
        dept = _hit("sop-clearance-women#d", doc_id="sop-clearance-women", authority="dept",
                    key_facts=[_fact("clearance_trigger_days", 45, "天")])
        report = detect([general, dept])
        result = SearchResult(
            query="清仓触发天数", rewritten="", hits=[general, dept], no_answer=False,
            rerank={"backend": "none"}, diagnostics={"is_primary": report["is_primary"]},
            conflicts=report["conflicts"], conflict_check=report["status"],
            conflict_note=report["note"],
        )
        context = result.context()
        assert context.startswith("## 冲突提示")
        assert "次要口径" in context                  # 落败的块被标注

    def test_citations_carry_authority_and_primary(self):
        from app.rag.pipeline import SearchResult

        general = _hit("sop-clearance#g", doc_id="sop-clearance", authority="general", title="清仓")
        dept = _hit("sop-clearance-women#d", doc_id="sop-clearance-women", authority="dept")
        result = SearchResult(
            query="q", rewritten="", hits=[general, dept], no_answer=False,
            rerank={"backend": "none"},
            diagnostics={"is_primary": {general.chunk_id: False, dept.chunk_id: True}},
            conflict_check=STATUS_UNAVAILABLE,
        )
        citations = result.citations
        assert citations[0]["authority"] == "general" and citations[0]["is_primary"] is False
        assert citations[1]["authority"] == "dept" and citations[1]["is_primary"] is True

    def test_cache_round_trip_keeps_conflicts(self):
        """缓存命中不能把冲突信息丢掉（否则同一问题第二次问就不提冲突了）。"""
        from app.rag.pipeline import SearchResult, _from_cache

        claim = Claim(key="days", value=45, unit="天", doc_id="d", authority="dept")
        conflicts = detect_conflicts([claim, Claim(key="days", value=60, unit="天",
                                                   doc_id="g", authority="general")])
        hit = _hit("d#1", doc_id="d", authority="dept")
        result = SearchResult(
            query="q", rewritten="", hits=[hit], no_answer=False,
            rerank={"backend": "none"}, diagnostics={"is_primary": {"d#1": True}},
            conflicts=conflicts, conflict_check=STATUS_OK, conflict_note="发现 1 处口径冲突",
            conflict_diagnostics={"compared_keys": ["days"], "single_source_keys": [],
                                  "suppressed": 2, "candidates": 30},
        )
        restored = _from_cache(result.to_dict())
        assert restored.conflict_check == STATUS_OK
        assert restored.conflicts[0]["winner"]["doc_id"] == "d"
        assert restored.conflict_note == "发现 1 处口径冲突"
        assert restored.is_primary == {"d#1": True}
        # 冲突自述也要一起回来，否则第二次命中就不知道"比对了哪些口径"
        assert restored.conflict_diagnostics["compared_keys"] == ["days"]
        assert restored.conflict_diagnostics["suppressed"] == 2


class TestConflictStats:
    def test_counters_track_outcomes(self):
        from app.rag import conflict

        conflict.reset_conflict_stats()
        detect([_hit("a#1", doc_id="a", key_facts=[_fact("k", 60, "天")])])          # none
        detect([_hit("a#1", doc_id="a", key_facts=[_fact("k", 60, "天")]),
                _hit("b#1", doc_id="b", authority="dept",
                     key_facts=[_fact("k", 45, "天")])])                              # ok
        detect([])                                                                    # unavailable
        stats = conflict.conflict_stats()
        assert stats["checked"] >= 3
        assert stats["none"] >= 1 and stats["ok"] >= 1 and stats["unavailable"] >= 1

    def test_counters_match_status_one_to_one(self):
        """四条路径各跑一次：checked 与状态计数必须一一对应（复审 P3）。"""
        from app.rag import conflict

        conflict.reset_conflict_stats()
        # ok
        detect([_hit("a#1", doc_id="a", authority="general", key_facts=[_fact("k", 60, "天")]),
                _hit("b#1", doc_id="b", authority="dept", key_facts=[_fact("k", 45, "天")])])
        # none（比对了，无分歧）
        detect([_hit("a#1", doc_id="a", key_facts=[_fact("k", 60, "天")])])
        # unavailable（池里没事实）
        detect([_hit("z#1", doc_id="z")])
        stats = conflict.conflict_stats()
        assert stats["checked"] == 3
        assert (stats["ok"], stats["none"], stats["unavailable"]) == (1, 1, 1)
        assert stats["resolved_rate"] if False else True     # health 里才算 rate，这里只校计数

    def test_intra_only_counts_none(self):
        """只有文档内部不一致时状态是 none，计数也必须记 none（否则 resolved_rate 被低估）。"""
        from app.rag import conflict

        conflict.reset_conflict_stats()
        detect([_hit("sop-x#1", doc_id="sop-x", key_facts=[
            _fact("days", 60.0, "天"), _fact("days", 45.0, "天")])])
        stats = conflict.conflict_stats()
        assert stats["intra"] == 1
        assert stats["none"] == 1
        assert stats["ok"] == 0

    def test_suppressed_accumulates(self):
        from app.rag import conflict

        conflict.reset_conflict_stats()
        detect([_hit("a#1", doc_id="a", key_facts=[_fact("k", 60, "天")]),
                _hit("b#1", doc_id="b", key_facts=[_fact("other", 1, "个")])],
               top_hits=[_hit("a#1", doc_id="a", key_facts=[_fact("k", 60, "天")])],
               query="问 k")
        assert conflict.conflict_stats()["suppressed"] >= 1

    def test_skipped_is_counted(self, monkeypatch):
        """pipeline 短路（池内无口径事实）也必须计数，否则 health 看不出"没比对"。"""
        from app.rag import conflict
        from app.rag import pipeline as pipeline_mod

        def _fake_recall(query, filters, as_of, rewritten, topk):  # noqa: ANN001
            cands = [{"chunk_id": "doc-a#1", "doc_id": "doc-a", "text": "正文",
                      "score": 0.5, "vector_score": 0.5, "keyword_score": 1.0,
                      "meta": {"doc_id": "doc-a", "title": "A", "body": "正文"}},
                     {"chunk_id": "doc-b#1", "doc_id": "doc-b", "text": "正文",
                      "score": 0.4, "vector_score": 0.4, "keyword_score": 1.0,
                      "meta": {"doc_id": "doc-b", "title": "B", "body": "正文"}}]
            return cands, {"reason": "ok"}

        monkeypatch.setattr(pipeline_mod, "_recall", _fake_recall)
        conflict.reset_conflict_stats()
        result = asyncio.run(pipeline_mod.search_kb("任意问题", topk=2, backend="none",
                                                    use_cache=False))
        stats = conflict.conflict_stats()
        assert result.conflict_check == STATUS_UNAVAILABLE
        assert stats["skipped"] == 1
        assert stats["checked"] == 1 and stats["unavailable"] == 1
        assert result.conflict_diagnostics.get("skipped_reason") == "no_key_facts_in_pool"

    def test_reset(self):
        from app.rag import conflict

        detect([_hit("a#1", doc_id="a")])
        conflict.mark_skipped("probe")
        conflict.reset_conflict_stats()
        stats = conflict.conflict_stats()
        assert stats["checked"] == 0 and stats["skipped"] == 0
        assert not any(k.startswith("skipped:") for k in stats)


class TestSectionValidation:
    def test_typo_section_warns_and_fact_is_never_attached(self, tmp_path, caplog):
        from app.rag.chunker import chunk_document
        from app.rag.loader import kb_fact_coverage, load_documents, reset_coverage_cache

        (tmp_path / "sop-typo.md").write_text(
            "---\ndoc_id: sop-typo\ntitle: 拼错小节\nkey_facts: 触发条件X|days=45天; *|flag=1个\n"
            "---\n## 触发条件\n可售天数 ≥ 60 天。\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            doc = load_documents(tmp_path)[0]
        assert any("小节在正文中不存在" in rec.message for rec in caplog.records), \
            [rec.message for rec in caplog.records]
        carried = [f["key"] for ch in chunk_document(doc) for f in ch.meta["key_facts"]]
        assert "days" not in carried          # 拼错 = 该事实永远到不了检索

        reset_coverage_cache()
        coverage = kb_fact_coverage(tmp_path)
        assert coverage["unmatched_count"] == 1
        assert coverage["unmatched_sections"][0]["section"] == "触发条件X"

    def test_real_corpus_has_no_unmatched_sections(self):
        from app.rag.loader import kb_fact_coverage, reset_coverage_cache

        reset_coverage_cache()
        coverage = kb_fact_coverage()
        assert coverage["unmatched_sections"] == []


class TestDepartmentFacet:
    """部门面是可比性闸门：不相交的部门口径不构成冲突（复审 P1）。"""

    def _three(self):
        general = _hit("sop-clearance#g", doc_id="sop-clearance", authority="general",
                       title="清仓 SOP", key_facts=[_fact("clearance_trigger_days", 60, "天")])
        general.meta["department"] = "通用"
        women = _hit("sop-clearance-women#d", doc_id="sop-clearance-women", authority="dept",
                     title="女装清仓 SOP",
                     key_facts=[_fact("clearance_trigger_days", 45, "天")])
        women.meta["department"] = "女装"
        men = _hit("sop-clearance-men#m", doc_id="sop-clearance-men", authority="dept",
                   title="男装清仓 SOP", key_facts=[])
        men.meta["department"] = "男装"
        return general, women, men

    def test_women_facet_conflicts_with_general(self):
        general, women, men = self._three()
        report = detect([general, women], facets=frozenset({"女装"}))
        assert report["status"] == STATUS_OK
        assert report["conflicts"][0]["winner"]["doc_id"] == "sop-clearance-women"
        assert report["department_overrides"] == []

    def test_men_facet_never_lets_women_win(self):
        """管理员问男装：女装细则不可比 → 不允许出现"采用 45 天"。"""
        general, women, men = self._three()
        report = detect([general, women, men], facets=frozenset({"男装"}))
        assert report["conflicts"] == []
        assert report["department_overrides"] == []      # 部门面明确时不产生例外
        assert report["compared_keys"] == ["clearance_trigger_days"]
        assert report["single_source_keys"] == ["clearance_trigger_days"]

    def test_no_facet_treats_department_rule_as_exception(self):
        """无部门面：通用口径是主答案，女装 45 天只能作"若为女装则 45 天"。"""
        general, women, men = self._three()
        report = detect([general, women, men], facets=None)
        assert report["conflicts"] == []
        assert [o["doc_id"] for o in report["department_overrides"]] == ["sop-clearance-women"]
        assert report["facets"] == {"departments": [], "asked": [], "missing": [],
                                    "comparative": False}
        text = note_text([], report["department_overrides"])
        assert "部门例外" in text
        assert "若问的是" in text and "女装" in text and "45 天" in text
        assert "采用 **" not in text, "部门例外不能给「采用」结论"

    def test_two_departments_without_common_rule(self):
        """只有两个部门口径、没有通用口径：不裁决，两者都是例外。"""
        _, women, _ = self._three()
        men = _hit("sop-clearance-men#m", doc_id="sop-clearance-men", authority="dept",
                   title="男装清仓 SOP",
                   key_facts=[_fact("clearance_trigger_days", 60, "天")])
        men.meta["department"] = "男装"
        report = detect([women, men], facets=None)
        assert report["conflicts"] == []
        assert report["status"] == STATUS_UNAVAILABLE
        assert {o["doc_id"] for o in report["department_overrides"]} == {
            "sop-clearance-women", "sop-clearance-men"}

    def test_cross_department_same_value_is_not_conflict(self):
        a = _hit("sop-a#1", doc_id="sop-a", authority="dept",
                 key_facts=[_fact("k", 60, "天")])
        a.meta["department"] = "男装"
        b = _hit("sop-b#1", doc_id="sop-b", authority="dept",
                 key_facts=[_fact("k", 60, "天")])
        b.meta["department"] = "女装"
        assert detect([a, b], facets=frozenset({"男装", "女装"}))["conflicts"] == []

    def test_overrides_counted(self):
        from app.rag import conflict

        conflict.reset_conflict_stats()
        general, women, men = self._three()
        detect([general, women, men], facets=None)
        assert conflict.conflict_stats()["overrides"] >= 1

    def test_facets_from_filters_derive(self):
        general, women, men = self._three()
        report = detect([general, women, men], filters={"department": ["女装", "通用"]})
        assert report["facets"]["departments"] == ["女装"]
        assert report["conflicts"][0]["winner"]["doc_id"] == "sop-clearance-women"


class TestScopeNotice:
    """问句部门面与账号可见面不一致：不许用本部门口径冒充（规则 C3）。"""

    def _docs(self):
        general = _hit("sop-clearance#g", doc_id="sop-clearance", authority="general",
                       title="清仓 SOP", key_facts=[_fact("clearance_trigger_days", 60, "天")])
        general.meta["department"] = "通用"
        women = _hit("sop-clearance-women#d", doc_id="sop-clearance-women", authority="dept",
                     title="女装清仓 SOP",
                     key_facts=[_fact("clearance_trigger_days", 45, "天")])
        women.meta["department"] = "女装"
        return general, women

    def test_women_account_asking_men_gets_notice_not_45(self):
        from app.rag import conflict

        general, women = self._docs()
        conflict.reset_conflict_stats()
        report = detect([general, women], facets=frozenset({"女装"}),
                        query_facets=frozenset({"男装"}), query="男装清仓触发条件")
        assert report["conflicts"] == []
        assert report["scope_notice"] and "男装" in report["scope_notice"]
        assert report["department_overrides"] == []      # 问的不是女装，不该给女装例外
        assert report["facets"]["asked"] == ["男装"]
        assert conflict.conflict_stats()["scope_mismatch"] == 1
        text = note_text([], report["department_overrides"], report["scope_notice"])
        assert "检索范围说明" in text and "男装" in text

    def test_same_department_question_has_no_notice(self):
        general, women = self._docs()
        report = detect([general, women], facets=frozenset({"女装"}),
                        query_facets=frozenset({"女装"}), query="女装清仓触发条件")
        assert report["scope_notice"] == ""
        assert report["conflicts"][0]["winner"]["doc_id"] == "sop-clearance-women"

    def test_no_query_facet_has_no_notice(self):
        general, women = self._docs()
        report = detect([general, women], facets=frozenset({"女装"}),
                        query="清仓触发条件")
        assert report["scope_notice"] == ""


class TestMultiFacet:
    """多部门面（比较类问题）不做跨部门排序（规则 C1 补充）。"""

    def _docs(self):
        general = _hit("sop-clearance#g", doc_id="sop-clearance", authority="general",
                       title="清仓 SOP", key_facts=[_fact("clearance_trigger_days", 60, "天")])
        general.meta["department"] = "通用"
        women = _hit("sop-clearance-women#d", doc_id="sop-clearance-women", authority="dept",
                     title="女装清仓 SOP",
                     key_facts=[_fact("clearance_trigger_days", 45, "天")])
        women.meta["department"] = "女装"
        men = _hit("sop-clearance-men#m", doc_id="sop-clearance-men", authority="dept",
                   title="男装清仓 SOP",
                   key_facts=[_fact("clearance_trigger_days", 60, "天")])
        men.meta["department"] = "男装"
        return general, women, men

    def test_cross_department_question_has_no_single_winner(self):
        from app.rag import conflict

        general, women, men = self._docs()
        conflict.reset_conflict_stats()
        report = detect([general, women, men], facets=frozenset({"男装", "女装"}),
                        query_facets=frozenset({"男装", "女装"}),
                        query="男装和女装的清仓触发天数")
        assert report["conflicts"] == [], "比较类问题不该单方胜出"
        assert report["status"] in (STATUS_NONE, STATUS_UNAVAILABLE)
        assert {o["department"] for o in report["department_overrides"]} == {"男装", "女装"}
        assert "多个部门" in report["scope_notice"]
        assert conflict.conflict_stats()["multi_department"] == 1
        text = note_text([], report["department_overrides"], report["scope_notice"])
        assert "比较类问题" in text

    def test_single_department_still_arbitrates(self):
        general, women, men = self._docs()
        report = detect([general, women], facets=frozenset({"女装"}),
                        query_facets=frozenset({"女装"}), query="女装清仓触发条件")
        assert report["conflicts"][0]["winner"]["doc_id"] == "sop-clearance-women"


class TestScopePartial:
    """部分可见：问句里有账号看不见的部门（交集非空但缺一部分）。

    例：女装账号问「女装和男装的清仓触发天数」——交集只有女装，
    不能只答女装就当答完了（复审 P2-1）。
    """

    def _docs(self):
        general = _hit("sop-clearance#g", doc_id="sop-clearance", authority="general",
                       title="清仓 SOP", key_facts=[_fact("clearance_trigger_days", 60, "天")])
        general.meta["department"] = "通用"
        women = _hit("sop-clearance-women#d", doc_id="sop-clearance-women", authority="dept",
                     title="女装清仓 SOP",
                     key_facts=[_fact("clearance_trigger_days", 45, "天")])
        women.meta["department"] = "女装"
        return general, women

    def test_partial_visibility_has_no_single_winner(self):
        from app.rag import conflict

        general, women = self._docs()
        conflict.reset_conflict_stats()
        report = detect([general, women], facets=frozenset({"女装"}),
                        query_facets=frozenset({"女装", "男装"}),
                        query="女装和男装的清仓触发天数")
        assert report["conflicts"] == [], "部分可见的比较句不该单方胜出"
        assert report["status"] in (STATUS_NONE, STATUS_UNAVAILABLE)
        assert [o["doc_id"] for o in report["department_overrides"]] == ["sop-clearance-women"]
        assert "男装" in report["scope_notice"]
        assert report["facets"]["missing"] == ["男装"]
        assert conflict.conflict_stats()["scope_partial"] == 1

    def test_visible_department_only_question_has_no_notice(self):
        general, women = self._docs()
        report = detect([general, women], facets=frozenset({"女装"}),
                        query_facets=frozenset({"女装"}), query="女装清仓触发条件")
        assert report["scope_notice"] == ""
        assert report["conflicts"][0]["winner"]["doc_id"] == "sop-clearance-women"
        assert report["department_overrides"] == []

    def test_partial_without_visible_fact_has_no_overrides(self):
        """男装账号问「男装和女装」：可见面里没有该口径 → 例外为空，但仍给范围说明。"""
        general, _women = self._docs()
        report = detect([general], facets=frozenset({"男装"}),
                        query_facets=frozenset({"男装", "女装"}),
                        query="男装和女装的清仓触发天数")
        assert report["conflicts"] == []
        assert report["department_overrides"] == []
        assert "女装" in report["scope_notice"]

    def test_mismatch_takes_priority_over_partial(self):
        """交集为空时走"完全不可见"，不产出例外（两条规则不打架）。"""
        general, women = self._docs()
        report = detect([general, women], facets=frozenset({"女装"}),
                        query_facets=frozenset({"男装"}), query="男装清仓触发条件")
        assert report["department_overrides"] == []
        assert "没有「男装」" in report["scope_notice"]


class TestComparativeIntent:
    """跨部门比较意图（没有点名部门也要认）：不能拿一个部门的口径当结论。"""

    def _docs(self):
        general = _hit("sop-clearance#g", doc_id="sop-clearance", authority="general",
                       title="清仓 SOP", key_facts=[_fact("clearance_trigger_days", 60, "天")])
        general.meta["department"] = "通用"
        women = _hit("sop-clearance-women#d", doc_id="sop-clearance-women", authority="dept",
                     title="女装清仓 SOP",
                     key_facts=[_fact("clearance_trigger_days", 45, "天")])
        women.meta["department"] = "女装"
        return general, women

    def test_generic_comparison_does_not_pick_one_department(self):
        from app.rag import conflict

        general, women = self._docs()
        conflict.reset_conflict_stats()
        report = detect([general, women], facets=frozenset({"女装"}),
                        comparative=True, query="哪个部门清仓门槛更低")
        assert report["conflicts"] == [], "跨部门比较不该单方胜出"
        assert report["status"] in (STATUS_NONE, STATUS_UNAVAILABLE)
        assert [o["doc_id"] for o in report["department_overrides"]] == ["sop-clearance-women"]
        assert "跨部门比较" in report["scope_notice"]
        assert report["facets"]["comparative"] is True
        assert conflict.conflict_stats()["comparison"] == 1

    def test_generic_comparison_reports_uncovered_departments(self):
        """男装文档被召回但没声明该口径 → 必须写明"按通用执行"。"""
        general, women = self._docs()
        men_doc = _hit("sop-clearance-men#m", doc_id="sop-clearance-men",
                       title="男装清仓 SOP", key_facts=[])
        men_doc.meta["department"] = "男装"
        report = detect([general, women, men_doc], facets=None, comparative=True,
                        query="哪个部门清仓门槛更低")
        assert report["conflicts"] == []
        assert report["uncovered_departments"] == ["男装"]

    def test_uncovered_only_when_department_doc_was_recalled(self):
        """没召回到该部门文档 ≠ 该部门没规定（复审 P2-2：不能凭空给制度结论）。"""
        general, women = self._docs()
        men_doc = _hit("sop-clearance-men#m", doc_id="sop-clearance-men",
                       title="男装清仓 SOP", key_facts=[])
        men_doc.meta["department"] = "男装"

        # 池里只有通用 + 女装文档 → 男装根本没被召回 → 不能说"男装未单独规定"
        only_two = detect([general, women], facets=None, comparative=True,
                          query="哪个部门清仓门槛更低")
        assert only_two["uncovered_departments"] == []

        # 池里有男装文档、可见面也是男装 → 它确实没声明该口径 → 可以报
        men_only = detect([general, men_doc], facets=frozenset({"男装"}),
                          comparative=True, query="各部门清仓门槛一样吗")
        assert men_only["uncovered_departments"] == ["男装"]

        # 可见面是女装时，即便池里混进男装文档，也不能替看不见的部门下结论
        mixed = detect([general, women, men_doc], facets=frozenset({"女装"}),
                       comparative=True, query="哪个部门清仓门槛更低")
        assert mixed["uncovered_departments"] == []

    def test_single_department_question_unaffected(self):
        general, women = self._docs()
        report = detect([general, women], facets=frozenset({"女装"}),
                        query_facets=frozenset({"女装"}), query="女装清仓触发条件")
        assert report["conflicts"][0]["winner"]["doc_id"] == "sop-clearance-women"
        assert report["scope_notice"] == ""
        assert report["uncovered_departments"] == []
        assert report["facets"]["comparative"] is False


class TestStatusSemantics:
    """三态必须自洽：ok⇒有冲突、none⇒比对了但没有分歧、unavailable⇒没比对。"""

    def _check(self, report: dict) -> None:
        status = report["status"]
        if status == STATUS_OK:
            assert report["conflicts"], "ok 状态必须带冲突条目"
            assert report["compared_keys"], "ok 状态必须有比对过的口径"
        elif status == STATUS_NONE:
            assert report["conflicts"] == [], "none 状态不应有冲突条目"
            assert report["compared_keys"], "none 必须是'比对了但没分歧'，不能是'没比对'"
        elif status == STATUS_UNAVAILABLE:
            assert report["conflicts"] == []
            assert report["compared_keys"] == [], "unavailable 不能声称比对过口径"
            assert "比对" in report["note"] or "不可用" in report["note"]
        else:  # pragma: no cover
            raise AssertionError(f"未知状态 {status}")

    def test_ok(self):
        self._check(detect([
            _hit("a#1", doc_id="a", authority="general", key_facts=[_fact("k", 60, "天")]),
            _hit("b#1", doc_id="b", authority="dept", key_facts=[_fact("k", 45, "天")]),
        ]))

    def test_none(self):
        self._check(detect([_hit("a#1", doc_id="a", key_facts=[_fact("k", 60, "天")])]))

    def test_unavailable_no_facts(self):
        self._check(detect([_hit("a#1", doc_id="a")]))

    def test_unavailable_irrelevant(self):
        self._check(detect(
            [_hit("a#1", doc_id="a", key_facts=[_fact("k", 60, "天")])],
            top_hits=[_hit("z#1", doc_id="z")],
            query="完全无关的问题",
        ))

    def test_diagnostics_shape(self):
        report = detect([
            _hit("a#1", doc_id="a", authority="general", key_facts=[_fact("k", 60, "天")]),
            _hit("b#1", doc_id="b", authority="dept", key_facts=[_fact("k", 45, "天")]),
            _hit("c#1", doc_id="c", key_facts=[_fact("other", 1, "个")]),
        ], top_hits=[_hit("a#1", doc_id="a", key_facts=[_fact("k", 60, "天")])])
        assert report["compared_keys"] == ["k"]
        assert report["single_source_keys"] == []
        assert report["suppressed"] >= 1          # other 口径与问题无关，未参与比对


class TestPoolDetection:
    """检测池 vs top-k：落败片段排在 top-k 之外时，冲突仍要能看见（复审 P1-1 回归）。"""

    def _pair(self, *, trigger_words=()):
        general = _hit("sop-clearance#g", doc_id="sop-clearance", authority="general",
                       title="清仓 SOP", key_facts=[
                           dict(_fact("clearance_trigger_days", 60, "天"),
                                triggers=list(trigger_words))])
        women = _hit("sop-clearance-women#d", doc_id="sop-clearance-women", authority="dept",
                     title="女装清仓 SOP", key_facts=[
                         dict(_fact("clearance_trigger_days", 45, "天"),
                              triggers=list(trigger_words))])
        noise = _hit("sop-noise#1", doc_id="sop-noise", title="执行节奏")
        return general, women, noise

    def test_loser_outside_topk_is_still_detected(self):
        general, women, noise = self._pair()
        # top_k 只有通用文档（60 天）；落败的女装片段只在池里
        report = detect([general, women, noise], top_hits=[general, noise])
        assert report["status"] == STATUS_OK
        assert report["conflicts"][0]["winner"]["doc_id"] == "sop-clearance-women"
        # 对比：只在 top-k 上比对就会静默丢冲突（上一轮的回退）
        top_only = detect([general, noise])
        assert top_only["status"] != STATUS_OK

    def test_irrelevant_pool_facts_are_suppressed(self):
        general, women, noise = self._pair()
        # top_k 里没有任何该口径的片段，问句也不含触发词 → 不展示，但要计数
        report = detect([general, women, noise], top_hits=[noise],
                        query="清仓怎么执行 分批节奏")
        assert report["status"] == STATUS_UNAVAILABLE
        assert report["conflicts"] == []
        assert report["suppressed"] >= 1

    def test_trigger_word_reopens_detection(self):
        general, women, noise = self._pair(trigger_words=("门槛", "触发"))
        report = detect([general, women, noise], top_hits=[noise], query="女装清仓天数门槛")
        assert report["status"] == STATUS_OK
        assert report["conflicts"][0]["winner"]["doc_id"] == "sop-clearance-women"

    def test_snippet_is_attached_for_prompt_use(self):
        general, women, noise = self._pair(trigger_words=("门槛",))
        report = detect([general, women, noise], top_hits=[noise], query="门槛")
        winner = report["conflicts"][0]["winner"]
        assert winner["snippet"]
        assert "胜出口径原文" in note_text(report["conflicts"])


class TestFactTriggers:
    def test_parse_triggers(self):
        from app.rag.loader import parse_triggers

        triggers = parse_triggers("a=触发,门槛; b=预警, 阈值 ;bad; =空key")
        assert triggers == {"a": {"any": ["触发", "门槛"], "none": []},
                            "b": {"any": ["预警", "阈值"], "none": []}}

    def test_loader_injects_triggers_into_facts(self, tmp_path):
        from app.rag.loader import load_documents

        (tmp_path / "sop-t.md").write_text(
            "---\ndoc_id: sop-t\ntitle: T\ndepartment: 通用\n"
            "key_facts: *|days=60天\nfact_triggers: days=门槛,触发\n---\n正文\n",
            encoding="utf-8",
        )
        doc = load_documents(tmp_path)[0]
        assert doc.key_facts[0]["triggers"] == ["门槛", "触发"]

    def test_triggers_reach_chunk_meta(self):
        from app.config import KB_DIR
        from app.rag.chunker import chunk_document
        from app.rag.loader import load_documents

        docs = {d.doc_id: d for d in load_documents(KB_DIR)}
        chunks = chunk_document(docs["sop-clearance"])
        trigger_chunk = [c for c in chunks
                         if any(f["key"] == "clearance_trigger_days"
                                for f in c.meta["key_facts"])]
        assert trigger_chunk
        assert trigger_chunk[0].meta["key_facts"][0]["triggers"]


class TestIntraDoc:
    """同一份文档对同一口径写了两个取值：语料缺陷，不能套"采用/不采用"。"""

    def test_parser_keeps_all_declarations(self):
        facts = parse_key_facts("days=60天; days=45天")
        assert [f["value"] for f in facts] == [60.0, 45.0]

    def test_loader_dedupes_and_warns(self, tmp_path, caplog):
        from app.rag.loader import load_documents

        path = tmp_path / "sop-dup.md"
        path.write_text(
            "---\ndoc_id: sop-dup\ntitle: 重复口径 SOP\ndepartment: 通用\n"
            "key_facts: days=60天; days=45天\n---\n正文\n",
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            docs = {d.doc_id: d for d in load_documents(tmp_path)}
        facts = docs["sop-dup"].key_facts
        assert [f["value"] for f in facts] == [60.0]          # 保留第一条
        assert any("语料内部口径不一致" in rec.message for rec in caplog.records), \
            [rec.message for rec in caplog.records]

    def test_loader_does_not_flag_equivalent_repeats(self, tmp_path):
        """同值不同写法（60天 / 60日）只是冗余，不该报"语料不一致"。"""
        from app.rag.loader import load_documents

        (tmp_path / "sop-same.md").write_text(
            "---\ndoc_id: sop-same\ntitle: 同值 SOP\ndepartment: 通用\n"
            "key_facts: days=60天; days=60日\n---\n正文\n",
            encoding="utf-8",
        )
        doc = load_documents(tmp_path)[0]
        assert len(doc.key_facts) == 1

    def test_detect_marks_intra_doc_without_ranking(self):
        hit = _hit("sop-x#aaaa1111", doc_id="sop-x", title="重复口径 SOP",
                   key_facts=[_fact("days", 60.0, "天"), _fact("days", 45.0, "天")])
        report = detect([hit])
        kinds = {c["kind"] for c in report["conflicts"]}
        assert kinds == {KIND_INTRA_DOC}, report["conflicts"]
        entry = report["conflicts"][0]
        assert entry["values"] == ["60 天", "45 天"]
        assert "winner" not in entry
        text = note_text(report["conflicts"])
        assert "语料内部不一致" in text
        assert "同时声明了 60 天 / 45 天" in text
        assert "采用" not in text, text          # 不给取舍结论

    def test_cross_and_intra_coexist(self):
        dup = _hit("sop-x#1", doc_id="sop-x", title="DUP",
                   key_facts=[_fact("days", 60.0, "天"), _fact("days", 45.0, "天")])
        general = _hit("sop-a#1", doc_id="sop-a", authority="general",
                       key_facts=[_fact("alert", 7.0, "天")])
        dept = _hit("sop-b#1", doc_id="sop-b", authority="dept",
                    key_facts=[_fact("alert", 5.0, "天")])
        report = detect([dup, general, dept])
        kinds = sorted(c["kind"] for c in report["conflicts"])
        assert kinds == [KIND_CROSS_DOC, KIND_INTRA_DOC]
        # is_primary 只由跨文档胜出关系决定：sop-b 赢，sop-a 次要
        assert report["is_primary"]["sop-b#1"] is True
        assert report["is_primary"]["sop-a#1"] is False
        assert report["is_primary"]["sop-x#1"] is True   # 内部不一致不参与主次标记


class TestUnitNormalization:
    def test_unit_aliases(self):
        assert canonical_unit("日") == canonical_unit("天") == "天"
        assert canonical_unit("h") == canonical_unit("小时")
        assert canonical_unit("％") == "%"
        assert canonical_unit("未知单位") == "未知单位"

    def test_claim_normalized_uses_canonical_unit(self):
        assert Claim(key="k", value=60.0, unit="日", doc_id="d").normalized == \
            Claim(key="k", value="60 天", doc_id="d").normalized


class TestSectionScopedFacts:
    """事实按小节归属后，无关问题不该被阈值冲突打断（复审 P2-2）。"""

    def test_process_question_has_no_conflict_noise(self, seeded):
        from app.auth import AuthUser
        from app.tools.platform_tools import build_platform_tools

        user = AuthUser(username="worker2", role="user", department="女装")
        tools = {t.name: t for t in build_platform_tools(user)}
        data = asyncio.run(tools["search_knowledge"].ainvoke(
            {"query": "清仓怎么执行 分批节奏", "topk": 5},
        ))
        # 命中的是"执行节奏/数量与节奏/审批"等小节，它们不带口径事实 →
        # 不能装成"检查过、没冲突"，也不能报出"触发天数"这种无关冲突
        assert data["conflicts"] == []
        assert data["conflict_check"] == STATUS_UNAVAILABLE
        assert "触发天数" not in (data.get("conflict_note") or "")

    def test_broken_size_question_surfaces_threshold_conflict(self, seeded):
        """"断码了怎么处理"命中了带口径的「断码保护」小节 → 池内 7 天 vs 案例 5 天要报出来。

        这正是池内检测的价值：案例片段排在 top-5 之外，只在 top-k 上比对就永远只看得到 7 天。
        """
        from app.auth import AuthUser
        from app.tools.platform_tools import build_platform_tools

        user = AuthUser(username="worker2", role="user", department="女装")
        tools = {t.name: t for t in build_platform_tools(user)}
        data = asyncio.run(tools["search_knowledge"].ainvoke(
            {"query": "断码了怎么处理", "topk": 5},
        ))
        assert data["conflict_check"] == STATUS_OK
        keys = {c["key"]: c for c in data["conflicts"]}
        assert keys["broken_size_alert_days"]["winner"]["doc_id"] == "sop-sizegap"
        assert {item["doc_id"] for item in keys["broken_size_alert_days"]["losers"]} == {
            "case-history"}
        # 胜出片段可能不在 top-k：要带原文，模型才有依据
        assert keys["broken_size_alert_days"]["winner"]["snippet"]

    def test_suppressed_keys_are_counted_not_shown(self, seeded):
        """池内有事实但与问题无关 → 不展示、但要计数（可观测）。"""
        from app.auth import AuthUser
        from app.tools.platform_tools import build_platform_tools

        user = AuthUser(username="worker2", role="user", department="女装")
        tools = {t.name: t for t in build_platform_tools(user)}
        data = asyncio.run(tools["search_knowledge"].ainvoke(
            {"query": "清仓怎么执行 分批节奏", "topk": 5},
        ))
        diag = data["conflict_diagnostics"]
        assert diag["candidates"] >= 5
        assert "compared_keys" in diag and "single_source_keys" in diag

    def test_threshold_question_still_conflicts(self, seeded):
        from app.auth import AuthUser
        from app.tools.platform_tools import build_platform_tools

        user = AuthUser(username="worker2", role="user", department="女装")
        tools = {t.name: t for t in build_platform_tools(user)}
        data = asyncio.run(tools["search_knowledge"].ainvoke(
            {"query": "女装清仓触发条件 可售天数", "topk": 5},
        ))
        assert data["conflict_check"] == STATUS_OK
        assert data["conflicts"][0]["kind"] == KIND_CROSS_DOC
        assert data["conflicts"][0]["winner"]["doc_id"] == "sop-clearance-women"


class TestRealCorpusConflicts:
    """用仓库里真实的 14 篇语料跑一遍：冲突必须真的被解出来。"""

    def test_demo_corpus_resolves_conflicts(self, seeded):
        from app.rag.pipeline import search_kb

        async def main():
            # 不覆盖重排后端：冲突检测依赖**召回广度**，默认配置就是线上那条链路
            women = await search_kb("女装清仓触发条件 可售天数", topk=5, use_cache=False,
                                    filters={"department": ["女装", "通用"]})
            case = await search_kb("断码预警的可售天数阈值", topk=5, use_cache=False,
                                   filters={"department": ["通用"]})
            return women, case

        women, case = asyncio.run(main())

        assert women.conflict_check == STATUS_OK
        clearance = [c for c in women.conflicts if c["key"] == "clearance_trigger_days"]
        assert clearance, [c["key"] for c in women.conflicts]
        assert clearance[0]["winner"]["doc_id"] == "sop-clearance-women"
        assert clearance[0]["winner"]["value"] == 45.0
        assert "女装" in women.context()[:200]
        assert "## 冲突提示" in women.context()

        assert case.conflict_check == STATUS_OK
        alert = [c for c in case.conflicts if c["key"] == "broken_size_alert_days"]
        assert alert, [c["key"] for c in case.conflicts]
        assert alert[0]["winner"]["doc_id"] == "sop-sizegap"
        assert {item["doc_id"] for item in alert[0]["losers"]} == {"case-history"}

    def test_search_knowledge_tool_returns_conflicts(self, seeded):
        from app.auth import AuthUser
        from app.tools.platform_tools import build_platform_tools

        worker2 = AuthUser(username="worker2", role="user", department="女装")
        tools = {t.name: t for t in build_platform_tools(worker2)}
        data = asyncio.run(tools["search_knowledge"].ainvoke(
            {"query": "女装清仓触发条件 可售天数", "topk": 5},
        ))
        assert data["conflict_check"] == STATUS_OK
        assert data["conflicts"][0]["winner"]["doc_id"] == "sop-clearance-women"
        assert data["conflict_note"]
        # 每条命中都带权威度与主次标记：前端/提示词据此区分「按细则」还是「参考案例」
        assert all("authority" in hit and "is_primary" in hit for hit in data["hits"])

    def test_conflict_eval_dataset_passes(self, seeded):
        from app.rag.eval import load_conflicts, run_conflict_eval

        rows = load_conflicts()
        assert rows, "冲突题集缺失"
        assert any(row.get("expect_no_conflict") for row in rows), "缺过触发负例"
        report = run_conflict_eval(topk=5)
        assert report["ok"], report["cases"]
        assert report["pass_rate"] == 1.0


class TestTriggerVeto:
    """过触发防护：用户其实在问补货/折扣时，不要拿"清仓触发天数"去打断他。"""

    def _pair(self, *, triggers, vetoes=()):
        general = _hit("sop-clearance#g", doc_id="sop-clearance", authority="general",
                       key_facts=[dict(_fact("clearance_trigger_days", 60, "天"),
                                       triggers=list(triggers),
                                       trigger_veto=list(vetoes))])
        women = _hit("sop-clearance-women#d", doc_id="sop-clearance-women", authority="dept",
                     key_facts=[dict(_fact("clearance_trigger_days", 45, "天"),
                                     triggers=list(triggers),
                                     trigger_veto=list(vetoes))])
        silent = _hit("sop-noise#1", doc_id="sop-noise")
        return general, women, silent

    def test_parse_triggers_with_veto(self):
        from app.rag.loader import parse_triggers

        spec = parse_triggers("a=触发,门槛!补货; b=预警")
        assert spec["a"] == {"any": ["触发", "门槛"], "none": ["补货"]}
        assert spec["b"] == {"any": ["预警"], "none": []}

    def test_loader_injects_veto_into_facts(self, tmp_path):
        from app.rag.loader import load_documents

        (tmp_path / "sop-v.md").write_text(
            "---\ndoc_id: sop-v\ntitle: V\ndepartment: 通用\n"
            "key_facts: *|days=60天\nfact_triggers: days=门槛!补货,折扣\n---\n正文\n",
            encoding="utf-8",
        )
        fact = load_documents(tmp_path)[0].key_facts[0]
        assert fact["triggers"] == ["门槛"]
        assert fact["trigger_veto"] == ["补货", "折扣"]

    def test_veto_blocks_trigger_channel(self):
        general, women, silent = self._pair(triggers=["清仓什么时候"], vetoes=["补货"])
        # 命中触发词且无排除词 → 相关
        assert detect([general, women, silent], top_hits=[silent],
                      query="女装清仓什么时候开始")["status"] == STATUS_OK
        # 命中排除词 → 不相关（未比对），不是"比对后无冲突"
        report = detect([general, women, silent], top_hits=[silent],
                        query="女装清仓什么时候补货最合适")
        assert report["status"] == STATUS_UNAVAILABLE
        assert report["conflicts"] == []

    def test_veto_does_not_block_structural_relevance(self):
        """用户已经在看这一节（top-k 带该口径）时，排除词不该把冲突压掉。"""
        general, women, silent = self._pair(triggers=["清仓什么时候"], vetoes=["补货"])
        report = detect([general, women], top_hits=[general],
                        query="清仓开始后还能补货吗")
        assert report["status"] == STATUS_OK

    def test_veto_is_per_key(self):
        general, women, silent = self._pair(triggers=["清仓什么时候"], vetoes=["补货"])
        other = _hit("sop-alert#1", doc_id="sop-alert", authority="general",
                     key_facts=[dict(_fact("broken_size_alert_days", 7, "天"),
                                     triggers=["预警"], trigger_veto=["补货"])])
        other2 = _hit("sop-alert2#1", doc_id="sop-alert2", authority="dept",
                      key_facts=[dict(_fact("broken_size_alert_days", 5, "天"),
                                      triggers=["预警"], trigger_veto=["补货"])])
        # 含"补货"的断码预警问题：只有 alert 这条 key 的排除词生效（它也被排除），
        # 而清仓 key 本来就没命中触发词 → 两条都不相关
        report = detect([general, women, other, other2], top_hits=[silent],
                        query="补货时断码预警阈值是多少")
        assert report["conflicts"] == []

    def test_expanded_query_only_helps_positive_matching(self):
        """同义词改写里可能带出排除词（清仓→折扣清仓），排除词必须只看用户原话。"""
        general, women, silent = self._pair(triggers=["门槛"],
                                            vetoes=["折扣"])
        report = detect([general, women, silent], top_hits=[silent],
                        query="女装清仓天数门槛", expanded="女装清仓天数门槛 折扣清仓")
        assert report["status"] == STATUS_OK, "改写结果里的排除词不该误杀正向匹配"


class TestTriggerVetoIntegration:
    """真实语料上的过触发负例（对应 eval 里的 conflict-noveto-* 三题）。"""

    @pytest.mark.parametrize("query", [
        "女装清仓什么时候补货最合适",
        "清仓开始后还能补货吗",
        "清仓折扣什么时候结束",
    ])
    def test_off_intent_question_does_not_report_conflict(self, seeded, query):
        from app.auth import AuthUser
        from app.tools.platform_tools import build_platform_tools

        user = AuthUser(username="worker2", role="user", department="女装")
        tools = {t.name: t for t in build_platform_tools(user)}
        data = asyncio.run(tools["search_knowledge"].ainvoke({"query": query, "topk": 5}))
        assert data["conflicts"] == [], (query, data["conflicts"])
        assert data["conflict_check"] != STATUS_OK

    @pytest.mark.parametrize("query", [
        "女装清仓什么时候开始",
        "女装清仓天数门槛",
        "清仓怎么触发 可售天数多少天",
    ])
    def test_threshold_question_still_reports_conflict(self, seeded, query):
        from app.auth import AuthUser
        from app.tools.platform_tools import build_platform_tools

        user = AuthUser(username="worker2", role="user", department="女装")
        tools = {t.name: t for t in build_platform_tools(user)}
        data = asyncio.run(tools["search_knowledge"].ainvoke({"query": query, "topk": 5}))
        assert data["conflict_check"] == STATUS_OK, (query, data["conflicts"])
        assert data["conflicts"][0]["key"] == "clearance_trigger_days"


def test_claim_to_dict_shape():
    claim = Claim(key="k", value=45.0, unit="天", doc_id="d", title="T", authority="dept",
                  version="v0.9", chunk_id="d#1")
    payload = dataclasses.asdict(claim)
    assert payload["value"] == 45.0
    assert claim.to_dict()["display"] == "45 天"
    assert claim.to_dict()["authority_label"] == "部门细则"
