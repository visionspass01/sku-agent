"""记忆 API 测试：Query 重写开关透传、会话记忆快照、组织档案部门隔离。"""

from __future__ import annotations

import pytest


@pytest.fixture()
def planning_chat(app_client, monkeypatch):
    """把 run_chat 换成立即返回 query_plan 的桩，专门验证接口层透传与展示开关。"""
    import app.api.chat_routes as chat_routes
    from app.agents.query_plan import build_query_plan
    from app.memory.runtime import current_chat_context

    async def fake_run_chat(user, history, message):
        ctx = current_chat_context()
        plan = build_query_plan(message, user=user)
        return {
            "reply": f"收到：{message}",
            "tool_trace": [],
            "metadata_version": 1,
            "query_plan": plan if ctx.get("include_query_plan") else None,
            "clarify": plan.get("clarify") if ctx.get("include_query_plan") else None,
            "cited_result_ids": ["res_deadbeef00"],
            "memory": {"facts_version": 1, "working_version": 2},
            "session_references": [],
        }

    monkeypatch.setattr(chat_routes, "run_chat", fake_run_chat)
    monkeypatch.setattr(chat_routes, "run_chat_stream", fake_run_chat)
    return app_client


def test_chat_returns_query_plan_only_when_enabled(planning_chat, login):
    admin = login("admin", "hhw123")
    on = planning_chat.post("/api/chat", headers=admin, json={
        "message": "ST-13-C1-M 是否断货，近7天", "include_query_plan": True,
    })
    assert on.status_code == 200
    plan = on.json()["query_plan"]
    assert plan and plan["intent"] == "diagnose"
    assert plan["entities"]["skus"] == ["ST-13-C1-M"]
    assert on.json()["cited_result_ids"] == ["res_deadbeef00"]
    assert on.json()["memory"]["working_version"] == 2

    off = planning_chat.post("/api/chat", headers=admin, json={"message": "ST-13-C1-M 库存"})
    assert off.status_code == 200
    assert off.json()["query_plan"] is None


def test_memory_snapshot_is_owner_only(app_client, login, monkeypatch):
    import app.api.chat_routes as chat_routes

    async def fake_run_chat(user, history, message):
        return {"reply": "ok", "tool_trace": [], "metadata_version": 1}

    monkeypatch.setattr(chat_routes, "run_chat", fake_run_chat)
    monkeypatch.setattr(chat_routes, "run_chat_stream", fake_run_chat)
    admin = login("admin", "hhw123")
    worker = login("worker1", "hhw111")
    sid = app_client.post("/api/chat", headers=admin, json={"message": "hi"}).json()["session_id"]

    mine = app_client.get(f"/api/memory/sessions/{sid}", headers=admin)
    assert mine.status_code == 200
    body = mine.json()
    assert set(["facts_card", "working_set", "latest_query_plan", "recent_tool_results"]) <= set(body)
    # 实体表的可展示文本（发送框下方紫标常驻区直接用），与注入系统提示的 <working-set> 同源
    assert "working_set_text" in body
    assert "updated_at" in body
    assert body["working_set_text"] == "" or "<working-set" in body["working_set_text"]
    assert app_client.get(f"/api/memory/sessions/{sid}", headers=worker).status_code == 403


def test_analyses_api_department_isolation(app_client, login):
    admin = login("admin", "hhw123")
    worker1 = login("worker1", "hhw111")
    worker2 = login("worker2", "hhw222")

    created = app_client.post("/api/memory/analyses", headers=admin, json={
        # 文案与 test_memory_layers 的 L3 用例区分开：那一条断言的是「同部门档案排第一」，
        # 两边都写「积压/调拨」会让 BM25 平分，排名取决于语料规模（加一个用例就翻车）
        "question": "男装品类近 30 天动销率复核",
        "conclusion": "动销率 0.62，低于阈值，建议核对上新节奏",
        "department": "男装",
        "key_metrics": {"available": 880},
        "evidence": ["res_aaaaaaaaaa"],
    })
    assert created.status_code == 200, created.text
    analysis_id = created.json()["analysis_id"]

    assert app_client.get(f"/api/memory/analyses/{analysis_id}", headers=worker1).status_code == 200
    assert app_client.get(f"/api/memory/analyses/{analysis_id}", headers=worker2).status_code == 404

    listing = app_client.get("/api/memory/analyses", headers=worker1, params={"q": "动销率 复核"})
    assert listing.status_code == 200
    assert all(item["department"] == "男装" for item in listing.json()["items"])

    forbidden = app_client.post("/api/memory/analyses", headers=worker2, json={
        "conclusion": "越权写入", "department": "男装",
    })
    assert forbidden.status_code == 403


def test_analyses_reject_unknown_department(app_client, login):
    """管理员也不能把档案写进任意部门目录（否则那种目录既无法检索也无人可见）。"""
    admin = login("admin", "hhw123")
    resp = app_client.post("/api/memory/analyses", headers=admin, json={
        "conclusion": "写进未知部门", "department": "财务部",
    })
    assert resp.status_code == 400
    assert "未知部门" in resp.json()["detail"]

    ok = app_client.post("/api/memory/analyses", headers=admin, json={
        "conclusion": "留空 = 全部门通用", "department": "",
    })
    assert ok.status_code == 200


def test_memory_endpoints_require_login(app_client):
    assert app_client.get("/api/memory/analyses").status_code == 401
    assert app_client.get("/api/memory/sessions/anything").status_code == 401
