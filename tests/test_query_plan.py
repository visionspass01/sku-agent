"""Query 理解与规划（query_plan）测试：确定性解析、可复现、权限前置、追问一次。"""

from __future__ import annotations

from app.agents.query_plan import build_query_plan, plan_brief
from app.auth import AuthUser


def test_plan_parses_sku_intent_and_window():
    plan = build_query_plan("ST-13-C1-M 是否面临断货风险，结合近7天订单和CRM预订给出影响")
    assert plan["intent"] == "diagnose"
    assert plan["entities"]["skus"] == ["ST-13-C1-M"]
    assert plan["entities"]["styles"] == ["ST-13"]
    assert plan["time_range"]["label"] == "近7天"
    assert plan["confidence"] >= 0.7
    assert plan["needs_clarify"] is False
    assert plan["plan_id"].startswith("qp_")
    assert plan["metric_version"]
    assert plan["tool_fingerprints"]


def test_plan_is_reproducible():
    msg = "帮我看看 ST-06-C1-M 是否需要补货"
    first = build_query_plan(msg)
    second = build_query_plan(msg)
    assert first["plan_id"] == second["plan_id"]
    assert first["entities"] == second["entities"]


def test_plan_intents_are_mutually_exclusive_priority():
    assert build_query_plan("帮我补货 ST-01-C1-M").get("intent") == "restock"
    assert build_query_plan("ST-01-C1-M 能不能跨仓调拨").get("intent") == "transfer"
    assert build_query_plan("跑一轮全量巡检").get("intent") == "patrol"
    assert build_query_plan("补货单列表里有哪些待确认").get("intent") == "approval"
    assert build_query_plan("断码 SOP 的口径是什么").get("intent") == "policy_qa"


def test_plan_resolves_reference_from_working_set():
    working_set = {"skus": {"ST-13-C1-M": {}}, "version": 3,
                   "time_range": {"label": "近7天"}}
    plan = build_query_plan("这个款还能撑几天？", working_set=working_set)
    assert plan["entities"]["skus"] == ["ST-13-C1-M"]
    assert plan["resolved_from"] == "working_set.skus"
    assert any("指代回填" in a for a in plan["assumptions"])
    # 回填得到的置信度应低于显式给出 SKU 的情况
    explicit = build_query_plan("这个款 ST-13-C1-M 还能撑几天？", working_set=working_set)
    assert explicit["confidence"] > plan["confidence"]


def test_plan_permission_gate_marks_blocked():
    worker = AuthUser(username="worker1", role="user", department="男装")
    plan = build_query_plan("女装 ST-91-C1-M 的库存情况", user=worker)
    assert plan["blocked"] is True
    assert plan["allowed_departments"] == ["男装"]
    assert plan["entities"]["asked_departments"] == ["女装"]
    admin = AuthUser(username="admin", role="admin", department=None)
    assert build_query_plan("女装 ST-91-C1-M 的库存情况", user=admin)["blocked"] is False


def test_plan_department_gate_uses_owning_department_not_token_match():
    """复审 A3：童装属于女装部门——女装账号问童装不算越权，男装账号问童装才算。"""
    worker2 = AuthUser(username="worker2", role="user", department="女装")
    worker1 = AuthUser(username="worker1", role="user", department="男装")
    ok = build_query_plan("童装 ST-90-C1-M 的库存情况", user=worker2)
    assert ok["blocked"] is False
    assert ok["entities"]["asked_departments"] == ["女装"]
    blocked = build_query_plan("童装 ST-90-C1-M 的库存情况", user=worker1)
    assert blocked["blocked"] is True


def test_plan_department_gate_survives_non_matching_platform_mapping(monkeypatch):
    """平台下发非同名映射（男装→上衣/裤装）时，问「男装」不能被误判成越权。"""
    from app.config import DEPARTMENT_CATEGORIES, set_department_categories

    original = {k: tuple(v) for k, v in DEPARTMENT_CATEGORIES.items()}
    set_department_categories({"男装": ("上衣",), "女装": ("裙装",)})
    try:
        worker1 = AuthUser(username="worker1", role="user", department="男装")
        worker2 = AuthUser(username="worker2", role="user", department="女装")
        assert build_query_plan("男装库存怎么样", user=worker1)["blocked"] is False
        assert build_query_plan("上衣库存怎么样", user=worker1)["blocked"] is False
        # 女装账号问男装品类 → 越权
        assert build_query_plan("上衣库存怎么样", user=worker2)["blocked"] is True
    finally:
        set_department_categories(original)


def test_plan_low_confidence_asks_once():
    plan = build_query_plan("帮我看下情况")
    assert plan["confidence"] < 0.7
    assert plan["needs_clarify"] is True
    assert plan["clarify"]["missing"]


def test_plan_intent_ignores_generic_polysemy():
    """复审 P2：裸「通过」是介词（"通过调拨能改善吗"），不是审批意图。"""
    assert build_query_plan("ST-01-C1-M 通过调拨能不能改善断码")["intent"] == "transfer"
    assert build_query_plan("ST-01-C1-M 通过什么方式能减少积压")["intent"] == "diagnose"
    # 真正的审批问法仍然要判对
    assert build_query_plan("补货单审批通过了吗")["intent"] == "approval"
    assert build_query_plan("这单审核通过了吗")["intent"] == "approval"


def test_plan_only_asks_clarify_when_entity_is_really_missing():
    """复审 P6：部门级/口径/清单类问题不需要 SKU，不该追问（那只是误导卡片）。"""
    for message in ("女装断码严重吗", "安全库存怎么算", "帮我看下待确认的补货单"):
        plan = build_query_plan(message)
        assert plan["needs_clarify"] is False, message
        assert plan["clarify"] is None, message
    # 纯问数、既无实体也无部门面：仍然追问一次
    need = build_query_plan("帮我看下情况")
    assert need["needs_clarify"] is True
    assert "SKU" in "、".join(need["clarify"]["missing"])


def test_plan_brief_only_appends():
    plan = build_query_plan("ST-02-C1-M 可用库存")
    brief = plan_brief(plan)
    assert "本轮查询计划" in brief
    assert plan["retrieval_query"]["original"] == "ST-02-C1-M 可用库存"
    assert plan["generative_rewrite"] is None  # 默认不做整句改写
