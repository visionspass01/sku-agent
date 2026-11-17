"""组织记忆（L3 档案）的部门归属与可见性（复审 F1/F2）。

背景：`save_analysis` 早先用"是谁在问"决定档案部门 —— admin 的 `user.department` 是 None，
于是**单部门**分析也被写成「全员」，而 `_visible` 把「全员」当公开 ⇒ 部门账号能读到别的
部门的 SKU 级结论（这跟项目在追问线程上"要校验数据来源"的原则冲突）。
现在部门跟着**内容**走：本轮取过的 SKU / 款号 / 部门级入参，谁的就归谁；跨部门才落「全员」。
"""

from __future__ import annotations

import asyncio


def _save_via_tool(monkeypatch, user, *, question, conclusion, evidence=None, steps=None):
    """走**真实的 Agent 工具**沉淀（含部门推断 + 写权限桩 + 台账），返回工具结果。"""
    from app.memory.runtime import append_turn_step, tool_context, turn_log_context
    from app.tools import platform_tools

    monkeypatch.setattr(platform_tools, "_spawn_background", lambda coro: coro.close())
    tools = {t.name: t for t in platform_tools.build_platform_tools(user)}

    async def _run():
        with tool_context(session_id="s_org", username=user.username,
                          department=user.department, agent_id="analyst"):
            with turn_log_context("t_org", "s_org"):
                for step in steps or []:
                    append_turn_step(step)
                return await tools["save_analysis"].ainvoke({
                    "question": question, "conclusion": conclusion,
                    "key_metrics": {"available": 20},
                    "evidence": evidence or [],
                })

    return asyncio.run(_run())


def _step(tool: str, args: dict) -> dict:
    """模拟一步取数（`steps[].args` 在真实链路里是 JSON 字符串）。"""
    import json

    return {"tool": tool, "args": json.dumps(args, ensure_ascii=False), "status": "ok",
            "result_id": "res_aaa111bbb2", "as_of": "2026-11-04", "elapsed_ms": 1}


def _search(departments: list[str], query: str = "清仓 补货") -> list[dict]:
    from app.db.engine import session_scope
    from app.memory import org_memory

    with session_scope() as session:
        return org_memory.search_analyses(session, query, departments=departments, limit=10)


def _get(analysis_id: str, departments: list[str] | None) -> dict | None:
    from app.db.engine import session_scope
    from app.memory import org_memory

    with session_scope() as session:
        return org_memory.get_analysis(session, analysis_id, departments=departments)


# ---------------------------------------------------------------- 写侧：部门按内容推断
def test_admin_archive_inherits_department_from_fetched_sku(seeded, monkeypatch, _settings):
    """admin 取过女装 SKU → 档案归女装，不再是「全员」。"""
    from app.auth import AuthUser

    result = _save_via_tool(
        monkeypatch, AuthUser(username="admin", role="admin", department=None),
        question="ST-90-C1-M 清仓怎么处理", conclusion="女装 ST-90 建议 8 折分批清仓",
        evidence=["res_aaa111bbb2"], steps=[_step("get_inventory", {"sku_id": "ST-90-C1-M"})],
    )

    assert result["department"] == "女装", result
    assert result["department_source"] == "inferred_sku"
    assert result["department_note"] == ""


def test_department_level_tool_call_also_carries_department(seeded, monkeypatch, _settings):
    """没有 SKU、但调了部门级工具（`department=男装`）→ 照样能推断。"""
    from app.auth import AuthUser

    result = _save_via_tool(
        monkeypatch, AuthUser(username="admin", role="admin", department=None),
        question="男装断码严重吗", conclusion="男装断码率 6%，集中在几个主推款",
        evidence=["res_aaa111bbb2"], steps=[_step("get_size_gap", {"department": "男装"})],
    )

    assert result["department"] == "男装", result
    assert result["department_source"] == "inferred_arg"


def test_company_wide_archive_stays_all_departments(seeded, monkeypatch, _settings):
    """真正没有部门证据的公司级结论 → 「全员」（语义保持：部门账号仍然可读）。"""
    from app.auth import AuthUser

    result = _save_via_tool(
        monkeypatch, AuthUser(username="admin", role="admin", department=None),
        question="整体动销率怎么样", conclusion="全公司动销率 62%，同比持平",
        evidence=["res_aaa111bbb2"], steps=[],
    )

    assert result["department"] == "全员", result
    assert result["department_source"] == "all"
    assert _get(result["analysis_id"], ["男装"]) is not None


def test_cross_department_archive_is_marked_and_warned(seeded, monkeypatch, _settings):
    """同时涉及两个部门 → 「全员」+ 明确的 note（让模型能转告用户）。"""
    from app.auth import AuthUser

    result = _save_via_tool(
        monkeypatch, AuthUser(username="admin", role="admin", department=None),
        question="ST-90-C1-M 与 ST-92-C1-M 的库存对比", conclusion="女装偏低、男装偏高",
        evidence=["res_aaa111bbb2"],
        steps=[_step("get_inventory", {"sku_id": "ST-90-C1-M"}),
               _step("get_inventory", {"sku_id": "ST-92-C1-M"})],
    )

    assert result["department"] == "全员", result
    assert result["department_source"] == "all"
    assert "跨部门" in result["department_note"]


def test_unresolvable_sku_is_reported_not_silently_widened(seeded, monkeypatch, _settings):
    """有 SKU 但目录查不到部门 → 仍然全员，但 note 要说明"目录查不到"，不能假装没证据。"""
    from app.auth import AuthUser

    result = _save_via_tool(
        monkeypatch, AuthUser(username="admin", role="admin", department=None),
        question="ST-77-C1-M 要不要补货", conclusion="建议补 60 件",
        evidence=["res_aaa111bbb2"], steps=[],
    )

    assert result["department"] == "全员"
    assert "目录查不到" in result["department_note"]


def test_department_account_archive_uses_own_department(seeded, monkeypatch, _settings):
    """部门账号（脚本直调场景）写作时按账号部门，不因内容里没有证据就变「全员」。"""
    from app.auth import AuthUser

    # 部门账号的工具是"写操作拒绝桩"，这里用 repair 后的同一函数走内部逻辑
    from app.tools import platform_tools

    dept, source, note = platform_tools._infer_archive_department(
        AuthUser(username="worker1", role="user", department="男装"),
        question="整体情况", conclusion="稳定", evidence=[],
    )
    assert dept == "男装" and source == "user" and note


# ---------------------------------------------------------------- 读侧：可见性矩阵
def test_cross_department_archive_is_invisible_to_other_department(seeded, monkeypatch, _settings):
    """女装档案对男装不可见（读/检索/@session 三条路径），对女装与 admin 可见。"""
    from app.auth import AuthUser
    from app.db.engine import session_scope
    from app.memory import org_memory

    result = _save_via_tool(
        monkeypatch, AuthUser(username="admin", role="admin", department=None),
        question="ST-91-C1-M 积压怎么处理", conclusion="女装 ST-91 建议先调拨再清仓",
        evidence=["res_aaa111bbb2"], steps=[_step("get_inventory", {"sku_id": "ST-91-C1-M"})],
    )
    analysis_id = result["analysis_id"]
    assert result["department"] == "女装"

    assert _get(analysis_id, ["男装"]) is None
    assert _get(analysis_id, ["女装", "童装"]) is not None
    assert _get(analysis_id, None) is not None
    # 检索结果里不该出现这份女装档案（同库可能还有别的用例写的档案，所以按 id 断言）
    assert analysis_id not in {h["analysis_id"] for h in _search(["男装"], "积压")}
    # @session 引用同一份档案：男装账号拿不到内容，而且状态是 denied（不是"没引用"）
    with session_scope() as session:
        state = org_memory.resolve_session_reference(
            session, f"@session {analysis_id} 还记得吗", departments=["男装"])
    assert state["status"] == "denied", state
    assert state["items"] == []


def test_worker_inference_cannot_leave_own_department(seeded, monkeypatch, _settings):
    """复审 S2：非管理员的部门推断必须落在**账号可写部门**内。

    早先只看内容：男装账号问一句女装 SKU，档案就会被写成「女装」。现在没有写权限所以打不通，
    但一旦 `WRITE_TOOLS_ROLES` 开放写权限、或脚本/手工路径复用同一个 helper 就会越界。
    """
    from app.auth import AuthUser
    from app.tools import platform_tools

    worker = AuthUser(username="worker1", role="user", department="男装")
    own = platform_tools._infer_archive_department(
        worker, question="ST-92-C1-M 积压怎么处理", conclusion="建议调拨", evidence=[])
    assert own[:2] == ("男装", "inferred_sku"), own

    crossed = platform_tools._infer_archive_department(
        worker, question="ST-90-C1-M 清仓怎么处理", conclusion="建议 8 折", evidence=[])
    assert crossed[0] == "男装" and crossed[1] == "user", crossed
    assert "只能写本部门" in crossed[2]

    # admin 不受限：跨部门内容照旧按内容归部门
    admin = AuthUser(username="admin", role="admin", department=None)
    assert platform_tools._infer_archive_department(
        admin, question="ST-90-C1-M 清仓怎么处理", conclusion="建议 8 折", evidence=[])[:2] == (
            "女装", "inferred_sku")


def test_department_account_cannot_write_archive(seeded, monkeypatch, _settings):
    """写权限桩：部门账号调 save_analysis 必须被拒绝（档案写入口不对部门开放）。"""
    from app.auth import AuthUser

    result = _save_via_tool(
        monkeypatch, AuthUser(username="worker1", role="user", department="男装"),
        question="ST-92-C1-M 积压", conclusion="建议调拨", evidence=[],
    )

    assert "无权限" in str(result.get("error") or ""), result
    assert result.get("error_kind") == "business"


def test_search_only_returns_own_department_plus_all(seeded, monkeypatch, _settings):
    """检索结果 = 本部门 + 「全员」，不含别的部门。"""
    from app.auth import AuthUser

    admin = AuthUser(username="admin", role="admin", department=None)
    women = _save_via_tool(
        monkeypatch, admin, question="ST-90-C1-M 清仓", conclusion="女装清仓 8 折",
        evidence=["res_aaa111bbb2"], steps=[_step("get_inventory", {"sku_id": "ST-90-C1-M"})],
    )
    men = _save_via_tool(
        monkeypatch, admin, question="ST-92-C1-M 补货", conclusion="男装补 120 件",
        evidence=["res_aaa111bbb2"], steps=[_step("get_inventory", {"sku_id": "ST-92-C1-M"})],
    )
    company = _save_via_tool(
        monkeypatch, admin, question="整体动销率", conclusion="全公司 62%",
        evidence=["res_aaa111bbb2"], steps=[],
    )

    ids_men = {h["analysis_id"] for h in _search(["男装"], "清仓 补货 动销")}
    assert men["analysis_id"] in ids_men
    assert company["analysis_id"] in ids_men          # 「全员」可见
    assert women["analysis_id"] not in ids_men        # 别部门的看不到

    ids_women = {h["analysis_id"] for h in _search(["女装", "童装"], "清仓 补货 动销")}
    assert women["analysis_id"] in ids_women
    assert men["analysis_id"] not in ids_women
    assert company["analysis_id"] in ids_women
