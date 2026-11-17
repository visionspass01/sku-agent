"""知识库可见性：路由与对话工具必须是**同一套**作用域（复审 P1-1）。

暴露过的问题：`/api/rag/search` 对管理员返回全量 SOP，而对话工具 `search_knowledge`
自己算了一遍部门过滤 —— `dept=None`（admin/匿名）被写成 `{"department": ["通用"]}`，
于是管理员在对话里只看到通用 SOP：
- 问「女装清仓触发天数」故意召回不到女装细则 → 只拿到通用 60 天；
- 更糟的是口径冲突检查返回 `none`（看起来"已核对、没冲突"）→ 模型自信地答错；
- 同一问题走 REST 却能召回女装细则并裁决成 45 天。

所以这里钉三件事：admin/匿名=全量、部门账号=本部门+通用、两条路径用同一个函数。
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture()
def admin_user():
    from app.auth import AuthUser

    return AuthUser(username="admin", role="admin", department=None)


@pytest.fixture()
def men_user():
    from app.auth import AuthUser

    return AuthUser(username="worker1", role="user", department="男装")


@pytest.fixture()
def women_user():
    from app.auth import AuthUser

    return AuthUser(username="worker2", role="user", department="女装")


def test_filter_is_single_source_for_admin_and_department(admin_user, men_user, women_user):
    from app.api.routes import _dept_filter
    from app.security.scope import kb_department_filter

    # 唯一实现：路由的 _dept_filter 必须与它完全一致
    assert _dept_filter(None) == kb_department_filter(None) == {}
    assert _dept_filter(admin_user) == kb_department_filter(admin_user) == {}
    assert kb_department_filter(men_user) == {"department": ["男装", "通用"]}
    assert kb_department_filter(women_user) == {"department": ["女装", "通用"]}

    # 部门字段为空的账号：不能拼出空字符串部门（否则会变成"只看通用"的静默降级）
    from app.auth import AuthUser

    blank = AuthUser(username="u0", role="user", department="")
    assert kb_department_filter(blank) == {"department": ["通用"]}


def test_scope_label_is_single_source(admin_user, men_user, women_user):
    """范围自述必须只有一句话：`全部 SOP` / `X 部门内部 SOP + 通用 SOP`（复审 P2-2）。"""
    from app.auth import AuthUser
    from app.security.scope import kb_scope_label

    assert kb_scope_label(None) == kb_scope_label(admin_user) == "全部 SOP"
    assert kb_scope_label(women_user) == "女装部门内部 SOP + 通用 SOP"
    assert kb_scope_label(men_user) == "男装部门内部 SOP + 通用 SOP"
    blank = AuthUser(username="u0", role="user", department="")
    assert kb_scope_label(blank) == "通用 SOP"


def test_scope_label_identical_between_tool_and_rest(app_client, login, seeded,
                                                    admin_user, women_user):
    """工具 `scope_used` 与 REST `scope` 必须逐字符相同（同一问题、同一账号）。"""
    query = "女装清仓触发条件 可售天数"
    for username, user, headers in (
        ("admin", admin_user, login("admin", "hhw123")),
        ("worker2", women_user, login("worker2", "hhw222")),
    ):
        tool_scope = _search(user, query)["scope_used"]
        rest_scope = app_client.get("/api/rag/search",
                                    params={"q": query}, headers=headers).json()["scope"]
        assert tool_scope == rest_scope, (username, tool_scope, rest_scope)


@pytest.mark.parametrize("query,expected_scope", [
    ("男装清仓触发条件", "男装部门内部 SOP + 通用 SOP（按问题部门收敛）"),
    ("女装清仓触发条件", "女装部门内部 SOP + 通用 SOP（按问题部门收敛）"),
    ("童装清仓什么时候开始", "女装部门内部 SOP + 通用 SOP（按问题部门收敛）"),
])
def test_admin_narrows_by_query_department(seeded, admin_user, query, expected_scope):
    """管理员问具体部门时，检索与裁决都收敛到「该部门 + 通用」（复审 P1）。"""
    from app.rag.facets import query_departments
    from app.security.scope import kb_department_filter

    facets = query_departments(query)
    assert facets, query
    filters = kb_department_filter(admin_user, facets)
    assert filters["department"][-1] == "通用"
    data = _search(admin_user, query)
    assert data["scope_used"] == expected_scope
    assert data["scope_used"] == _search(admin_user, query)["scope_used"]


def test_admin_men_question_never_gets_women_threshold(seeded, admin_user):
    """男装问题不允许出现「女装 45 天」这个胜出口径（本轮修的 P1）。"""
    data = _search(admin_user, "男装清仓触发条件")
    assert data["conflicts"] == []
    assert all("sop-clearance-women" not in c.get("winner", {}).get("doc_id", "")
               for c in data["conflicts"])
    assert data["department_overrides"] == []          # 部门面明确时不产生例外


def test_admin_no_facet_reports_department_exception(seeded, admin_user):
    """无部门面：通用口径为主，女装细则只作"若为女装则 45 天"。"""
    data = _search(admin_user, "清仓触发条件")
    assert data["conflicts"] == []
    overrides = data["department_overrides"]
    assert [o["doc_id"] for o in overrides] == ["sop-clearance-women"]
    assert overrides[0]["department"] == "女装"
    assert overrides[0]["display"] == "45 天"


def test_same_question_across_accounts_is_consistent(seeded, admin_user, men_user, women_user):
    """同一句问法：管理与男装账号应得通用口径，女装账号才得部门细则。"""
    query = "清仓触发条件"
    admin_conflicts = _search(admin_user, query)["conflicts"]
    men_conflicts = _search(men_user, query)["conflicts"]
    women = _search(women_user, query)

    assert admin_conflicts == [], "管理员问通用问题不该被女装细则压过"
    assert men_conflicts == []
    assert women["conflicts"][0]["winner"]["doc_id"] == "sop-clearance-women"
    assert women["conflicts"][0]["winner"]["value"] == 45.0
    # 男装账号问同一句：不应出现女装口径（此前 admin 与 worker1 会出现 45/60 分叉）
    assert all("women" not in c["winner"]["doc_id"] for c in men_conflicts)


def _search(user, query: str, topk: int = 5) -> dict:
    from app.tools.platform_tools import build_platform_tools

    tools = {t.name: t for t in build_platform_tools(user)}
    return asyncio.run(tools["search_knowledge"].ainvoke({"query": query, "topk": topk}))


def test_admin_recalls_department_docs_and_sees_conflict(seeded, admin_user):
    """管理员问具体部门时能召回该部门细则（并按部门面收敛范围）。"""
    data = _search(admin_user, "女装清仓触发条件 可售天数")
    doc_ids = {hit["doc_id"] for hit in data["hits"]}
    assert "sop-clearance-women" in doc_ids, doc_ids
    # 检索范围自述：问句含部门面 → 收敛到「女装 + 通用」（与 REST 的 scope 文案一致）
    assert data["scope_used"] == "女装部门内部 SOP + 通用 SOP（按问题部门收敛）"
    assert data["conflict_check"] == "ok"
    keys = {c["key"]: c for c in data["conflicts"]}
    assert keys["clearance_trigger_days"]["winner"]["doc_id"] == "sop-clearance-women"
    assert keys["clearance_trigger_days"]["winner"]["value"] == 45.0
    # REST 侧走同一套收敛逻辑，必须得到同样的胜出方
    from app.rag.facets import query_departments
    from app.rag.pipeline import search_kb
    from app.security.scope import kb_department_filter

    rest = asyncio.run(search_kb("女装清仓触发条件 可售天数",
                                 filters=kb_department_filter(
                                     admin_user, query_departments("女装清仓触发条件 可售天数")),
                                 topk=5, use_cache=False))
    rest_winner = {c["key"]: c["winner"]["doc_id"] for c in rest.conflicts}
    assert rest_winner.get("clearance_trigger_days") == "sop-clearance-women"


def test_admin_no_department_word_keeps_full_scope(seeded, admin_user):
    """问句没有部门面时保持全量（`全部 SOP`），此时女装细则只能作例外。"""
    data = _search(admin_user, "清仓触发条件")
    assert data["scope_used"] == "全部 SOP"
    assert data["conflicts"] == []
    assert [o["doc_id"] for o in data["department_overrides"]] == ["sop-clearance-women"]


def test_department_account_asking_other_department_gets_notice(seeded, men_user, women_user):
    """部门账号问他人部门：不得用本部门口径冒充，必须给范围说明（规则 C3）。"""
    women_asks_men = _search(women_user, "男装清仓触发条件是什么")
    assert women_asks_men["conflicts"] == [], women_asks_men["conflicts"]
    assert "男装" in women_asks_men["scope_notice"]
    assert women_asks_men["conflict_check"] != "ok"

    men_asks_women = _search(men_user, "女装清仓触发条件是什么")
    assert men_asks_women["conflicts"] == []
    assert "女装" in men_asks_women["scope_notice"]


def test_own_department_question_has_no_notice(seeded, women_user):
    data = _search(women_user, "女装清仓触发条件是什么")
    assert data["scope_notice"] == ""
    assert data["conflicts"][0]["winner"]["doc_id"] == "sop-clearance-women"


def test_cross_department_comparison_is_not_arbitrated(seeded, admin_user):
    """比较类问题：各部门口径分别给出，不产生单一胜出（规则 C1 补充）。"""
    data = _search(admin_user, "男装和女装的清仓触发天数是天数")
    assert data["conflicts"] == []
    assert "多个部门" in data["scope_notice"]
    assert [o["doc_id"] for o in data["department_overrides"]] == ["sop-clearance-women"]


def test_partial_visibility_comparison_across_accounts(seeded, men_user, women_user):
    """部门账号问"本部门 + 他部门"：不单方胜出，并点名看不见的部门（复审 P2-1）。"""
    women = _search(women_user, "女装和男装的清仓触发天数是几天")
    assert women["conflicts"] == []
    assert "男装" in women["scope_notice"]
    assert [o["doc_id"] for o in women["department_overrides"]] == ["sop-clearance-women"]

    men = _search(men_user, "男装和女装的清仓触发天数是几天")
    assert men["conflicts"] == []
    assert "女装" in men["scope_notice"]
    assert men["department_overrides"] == []          # 男装细则未声明该口径 → 无例外


def test_partial_visibility_never_lets_one_department_win(seeded, women_user):
    """比较句里可见部门的 45 天只能是"例外"，不能是 winner。"""
    data = _search(women_user, "女装的清仓触发天数比男装低吗")
    assert data["conflicts"] == []
    assert all(c["winner"]["doc_id"] != "sop-clearance-women" for c in data["conflicts"])
    assert "男装" in data["scope_notice"]


def test_generic_comparison_question_is_flagged(seeded, men_user, admin_user):
    """「各部门一样吗」「哪个部门」这类问法没有点名部门，也必须给范围说明（复审 P2-1）。"""
    men = _search(men_user, "清仓触发天数各部门一样吗")
    assert men["conflicts"] == []
    assert "跨部门比较" in men["scope_notice"]
    assert "不要据此断言" in men["scope_notice"]
    assert men["uncovered_departments"] == ["男装"]      # 男装没声明该口径

    admin = _search(admin_user, "哪个部门清仓门槛更低")
    assert admin["conflicts"] == []
    assert "跨部门比较" in admin["scope_notice"]
    assert [o["doc_id"] for o in admin["department_overrides"]] == ["sop-clearance-women"]
    assert admin["uncovered_departments"] == ["男装"]


def test_single_department_question_has_no_comparison_noise(seeded, women_user):
    data = _search(women_user, "女装清仓触发条件")
    assert data["scope_notice"] == ""
    assert data["uncovered_departments"] == []


def test_men_question_consistent_across_accounts(seeded, admin_user, men_user, women_user):
    """同一道男装题：管理员与男装账号都不该得到女装 45 天；女装账号要说明看不见男装。"""
    query = "男装清仓触发条件"
    admin = _search(admin_user, query)
    men = _search(men_user, query)
    women = _search(women_user, query)
    for data in (admin, men, women):
        assert all("women" not in c["winner"]["doc_id"] for c in data["conflicts"]), data
    assert "男装" in women["scope_notice"] or women["conflict_check"] != "ok"


def test_department_user_still_isolated(seeded, men_user, women_user):
    men = _search(men_user, "清仓触发条件 可售天数")
    men_docs = {hit["doc_id"] for hit in men["hits"]}
    assert men_docs, "男装账号检索不到任何 SOP"
    assert not any(doc.endswith("-women") for doc in men_docs), men_docs

    women = _search(women_user, "女装清仓触发条件 可售天数")
    women_docs = {hit["doc_id"] for hit in women["hits"]}
    assert "sop-clearance-women" in women_docs
    assert not any(doc.endswith("-men") for doc in women_docs), women_docs
    assert women["conflict_check"] == "ok"
    assert women["scope_used"] == "女装部门内部 SOP + 通用 SOP"


def test_rag_search_endpoint_scope_matches_tool(app_client, login, seeded):
    """REST 的 `scope` 自述与工具一致：含部门面时收敛，不含时全量。"""
    headers = login("admin", "hhw123")
    narrowed = app_client.get("/api/rag/search",
                              params={"q": "女装清仓触发条件 可售天数"}, headers=headers).json()
    assert narrowed["scope"] == "女装部门内部 SOP + 通用 SOP（按问题部门收敛）"
    assert "sop-clearance-women" in {hit["doc_id"] for hit in narrowed["hits"]}
    assert narrowed["conflict_check"] == "ok"

    full = app_client.get("/api/rag/search", params={"q": "清仓触发条件"},
                          headers=headers).json()
    assert full["scope"] == "全部 SOP"
    assert [o["doc_id"] for o in full["department_overrides"]] == ["sop-clearance-women"]
