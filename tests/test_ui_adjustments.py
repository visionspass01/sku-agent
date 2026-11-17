"""本轮交互调整的回归测试：巡检单框、功能介绍动态显示、24h 会话保留、SSE 鉴权、拍立淘 Mock 标注。"""

from __future__ import annotations

import datetime as dt


# ---------------------------------------------------------------- 批量巡检：单框 + 去解释文字
def test_patrol_panel_single_status_box():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    html = (root / "web/index.html").read_text(encoding="utf-8")
    js = (root / "web/app.js").read_text(encoding="utf-8")
    assert 'id="patrol-status"' in html
    assert 'id="patrol-log"' not in html
    assert 'id="patrol-progress"' not in html
    # 解释文字已移除
    assert "定时巡检：已关闭" not in html
    assert "无人访问也耗 token" not in html
    assert 'id="scheduler-badge"' not in html
    # 状态框相关代码保留
    assert "setPatrolStatus" in js and "PATROL_IDLE_TEXT" in js


def test_render_anonymous_does_not_write_login_prompt_to_patrol():
    """登录态冲突根因：匿名渲染曾往巡检框写"未运行（登录后可触发巡检）"。"""
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "web/app.js").read_text(encoding="utf-8")
    assert "未运行（登录后可触发巡检）" not in js
    assert "未登录：可浏览历史巡检记录，触发巡检需登录" in js


# ---------------------------------------------------------------- 功能介绍：按勾选动态显示
def test_feature_hints_static_structure():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    html = (root / "web/index.html").read_text(encoding="utf-8")
    js = (root / "web/app.js").read_text(encoding="utf-8")
    css = (root / "web/style.css").read_text(encoding="utf-8")
    assert 'id="feature-hints"' in html and "feature-hints hidden" in html
    assert 'id="query-plan-hint"' not in html
    assert "renderFeatureHints" in js
    assert "FEATURE_HINT_TEXT" in js
    assert ".feature-hint" in css


# ---------------------------------------------------------------- 历史会话 24h 保留
def _reset_chat_state(username: str) -> None:
    """清掉该账号的历史会话（含子表）。

    为什么需要：`/api/chat/sessions` 只返回**最近 20 条**（按 updated_at 倒序），
    别的用例留下的会话一多，本文件"某个会话应该在列表里"的断言就会随执行顺序飘。
    清干净后，这些用例只依赖自己造的数据。
    """
    from sqlalchemy import delete, select

    from app.db.engine import session_scope
    from app.db.models import ChatMemory, ChatMessage, ChatQueryPlan, ChatSession, ChatTurnLog

    with session_scope() as session:
        ids = list(session.scalars(select(ChatSession.id).where(ChatSession.username == username)))
        if not ids:
            return
        for table in (ChatMessage, ChatTurnLog, ChatQueryPlan, ChatMemory):
            session.execute(delete(table).where(table.session_id.in_(ids)))
        session.execute(delete(ChatSession).where(ChatSession.id.in_(ids)))
        session.flush()


def _make_session(session, username: str, hours_ago: float):
    from app.agents import chat_store

    row = chat_store.create_session(session, username)
    msg = chat_store.append_message(session, row.id, "user", "老会话")
    # append_message 会刷新 updated_at，所以放到最后再回拨时间
    stamp = dt.datetime.now() - dt.timedelta(hours=hours_ago)
    row.updated_at = stamp
    row.created_at = stamp
    session.flush()
    return row.id, msg.id


def test_list_sessions_within_hours(seeded):
    from app.agents import chat_store
    from app.db.engine import session_scope

    _reset_chat_state("admin")
    with session_scope() as session:
        old_id, _ = _make_session(session, "admin", 30)
        fresh_id, _ = _make_session(session, "admin", 1)
    with session_scope() as session:
        listed = [s.id for s in chat_store.list_sessions(session, "admin")]
        assert fresh_id in listed and old_id in listed  # 不带时间窗时都返回
        filtered = [s.id for s in chat_store.list_sessions(session, "admin", within_hours=24)]
        assert fresh_id in filtered
        assert old_id not in filtered


def test_prune_sessions_deletes_children(seeded):
    from app.agents import chat_store
    from app.db.engine import session_scope
    from app.db.models import ChatMessage, ChatSession, ChatTurnLog

    with session_scope() as session:
        old_id, msg_id = _make_session(session, "admin", 30)
        chat_store.save_turn_log(session, old_id, msg_id, "admin",
                                 {"turn_id": "t_old", "plan": {}, "memory": {}, "steps": []})
    with session_scope() as session:
        pruned = chat_store.prune_sessions(session, 24)
        assert pruned >= 1
        assert session.get(ChatSession, old_id) is None
        assert session.query(ChatMessage).filter_by(session_id=old_id).count() == 0
        assert session.query(ChatTurnLog).filter_by(session_id=old_id).count() == 0


def test_sessions_endpoint_applies_retention(app_client, login):
    from app.db.engine import session_scope

    _reset_chat_state("admin")
    with session_scope() as session:
        old_id, _ = _make_session(session, "admin", 30)
        keep_id, _ = _make_session(session, "admin", 2)
    admin = login("admin", "hhw123")
    data = app_client.get("/api/chat/sessions", headers=admin).json()
    assert data["retention_hours"] == 24
    ids = [it["session_id"] for it in data["items"]]
    assert keep_id in ids and old_id not in ids
    assert data["pruned"] >= 1
    # 老会话已被物理清理 → 回放 404
    assert app_client.get(f"/api/chat/{old_id}", headers=admin).status_code == 404


# ---------------------------------------------------------------- SSE：前端请求要带鉴权
def test_stream_chat_sends_authorization():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "web/app.js").read_text(encoding="utf-8")
    idx = js.index("async function streamChat")
    segment = js[idx: idx + 700]
    assert "authHeaders(" in segment
    assert "Authorization" in js
    assert "HTTP 401" in segment or "401" in segment


def test_sse_endpoint_works_with_token(app_client, login, monkeypatch):
    import app.api.chat_routes as chat_routes

    async def fake_run_chat(user, history, message):
        return {"reply": "ok", "tool_trace": [], "metadata_version": 1, "query_plan": None,
                "clarify": None, "cited_result_ids": [], "memory": {},
                "session_references": [], "run_detail": None}

    monkeypatch.setattr(chat_routes, "run_chat", fake_run_chat)
    monkeypatch.setattr(chat_routes, "run_chat_stream", fake_run_chat)
    admin = login("admin", "hhw123")
    with app_client.stream("POST", "/api/chat/stream", json={"message": "hi"}, headers=admin) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        body = "".join(resp.iter_text())
    assert "event: done" in body
    # 不带 token 必须 401（前端之前就是漏了这个头才降级）
    assert app_client.post("/api/chat/stream", json={"message": "hi"}).status_code == 401


# ---------------------------------------------------------------- 拍立淘 Mock 标注
def test_about_marks_mcp_as_mock():
    from pathlib import Path

    about = (Path(__file__).resolve().parent.parent / "docs/ABOUT.md").read_text(encoding="utf-8")
    assert "ERP 与拍立淘均为本地 Mock 数据源" in about
    assert "拍立淘图片检索 API" in about


def test_about_md_route_serves_docs(app_client):
    """项目简介文档放在 docs/，通过 /about.md 路由提供给前端渲染。"""
    resp = app_client.get("/about.md")
    assert resp.status_code == 200
    assert "text/markdown" in resp.headers.get("content-type", "")
    assert "2.2 检索向量方案" in resp.text, "应读出 docs/ABOUT.md 的章节内容"
    assert "no-cache" in resp.headers.get("cache-control", "")
