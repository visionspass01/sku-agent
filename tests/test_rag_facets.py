"""适用面（部门面）：冲突裁决前的"可比性闸门"（复审 P1 / 规则 C1-C3）。

背景：权威度阶梯说的是"同一适用面内谁优先"，它回答不了"这条规则适不适用"。
两者混在一起就会出现——管理员问「男装清仓触发条件」，男装细则没声明该口径，
裁决变成「女装 45 天 vs 通用 60 天」→ 女装胜出 → **男装问题被答成 45 天**。
所以部门面必须先做过滤，再谈权威度。
"""

from __future__ import annotations

import pytest

from app.rag.facets import (
    claim_applicable,
    facets_from_filters,
    is_common,
    query_departments,
)


class TestQueryDepartments:
    @pytest.mark.parametrize("message,expected", [
        ("男装清仓触发条件", {"男装"}),
        ("女装清仓触发条件", {"女装"}),
        ("童装清仓什么时候开始", {"女装"}),           # 童装归女装部门（config 映射）
        ("男士夹克断码怎么补", {"男装"}),
        ("女士外套积压", {"女装"}),
        ("男装和女装的清仓阈值一样吗", {"男装", "女装"}),
        ("清仓触发条件是什么", set()),                # 识别不到 = 无部门面（保守）
        ("", set()),
    ])
    def test_detect(self, message, expected):
        assert set(query_departments(message)) == expected


class TestFacetsFromFilters:
    def test_none_when_no_filters(self):
        assert facets_from_filters(None) is None
        assert facets_from_filters({}) is None
        assert facets_from_filters({"category": ["女装"]}) is None

    def test_strips_common(self):
        assert facets_from_filters({"department": ["女装", "通用"]}) == frozenset({"女装"})
        assert facets_from_filters({"department": "男装"}) == frozenset({"男装"})
        # 只有通用 = 没有部门面（管理员问通用问题）
        assert facets_from_filters({"department": ["通用"]}) is None


class TestApplicability:
    def test_common_always_applicable(self):
        assert is_common("通用") and is_common("全部") and is_common("")
        assert claim_applicable("通用", None)
        assert claim_applicable("通用", frozenset({"女装"}))

    def test_department_doc_requires_matching_facet(self):
        assert claim_applicable("女装", frozenset({"女装"}))
        assert not claim_applicable("女装", frozenset({"男装"}))
        # 无部门面：部门文档只能是"例外"，不能参与裁决（这就是 P1 的修复点）
        assert not claim_applicable("女装", None)


class TestComparativeIntent:
    @pytest.mark.parametrize("message", [
        "哪个部门清仓门槛更低",
        "各部门清仓触发天数分别是多少",
        "清仓触发天数各部门一样吗",
        "跨部门对比一下清仓阈值",
        "男装和女装的部门之间有什么差异",
    ])
    def test_comparative_detected(self, message):
        from app.rag.facets import is_comparative

        assert is_comparative(message), message

    @pytest.mark.parametrize("message", [
        # 通用比较词**不算**部门比较意图：这些问句跟部门无关（复审 P2-1）
        "清仓和补货的区别是什么",
        "对比一下两个供应商的交期",
        "分别列出断码的 SKU",
        "折扣和清仓相比哪个更划算",
        "两种调拨方案哪个更好",
        "女装和男装有什么区别",      # 点名多个部门，由 len(asked) > 1 判定为比较句
    ])
    def test_generic_comparison_words_not_flagged(self, message):
        from app.rag.facets import is_comparative

        assert is_comparative(message) is False, message

    def test_specific_departments_are_handled_by_facet_count(self):
        """"男装和女装"靠问句部门面计数（>1）判定为比较句，不依赖比较意图词。"""
        from app.rag.facets import is_comparative, query_departments

        message = "男装和女装的清仓触发天数"
        assert len(query_departments(message)) == 2
        assert is_comparative(message) is False

    @pytest.mark.parametrize("message", [
        "女装清仓触发条件",
        "清仓怎么执行 分批节奏",
        "补货的目标覆盖天数怎么算",
        "",
    ])
    def test_non_comparative_not_flagged(self, message):
        from app.rag.facets import is_comparative

        assert not is_comparative(message), message


class TestOrderedDepartments:
    def test_order_follows_config_not_codepoint(self):
        from app.security.scope import ordered_departments

        # 按码位排会是「女装、男装」；配置顺序是 男装 → 女装
        assert ordered_departments(["女装", "男装"]) == ["男装", "女装"]
        assert ordered_departments({"女装"}) == ["女装"]

    def test_unknown_departments_appended_and_deduped(self):
        from app.security.scope import ordered_departments

        assert ordered_departments(["女装", "配饰", "女装", "男装"]) == ["男装", "女装", "配饰"]

    def test_scope_label_uses_config_order(self):
        from app.auth import AuthUser
        from app.security.scope import kb_scope_label

        admin = AuthUser(username="admin", role="admin", department=None)
        assert kb_scope_label(admin, ["女装", "男装"]) == \
            "男装、女装部门内部 SOP + 通用 SOP（按问题部门收敛）"
