"""多轮会话存储与对话接口鉴权测试。"""

from __future__ import annotations

from app.agents import chat_store
from app.db.engine import session_scope


def test_session_isolation(seeded):
    with session_scope() as session:
        s1 = chat_store.create_session(session, "admin")
        s2 = chat_store.create_session(session, "worker1")
        sid1, sid2 = s1.id, s2.id
        chat_store.append_message(session, sid1, "user", "查询 ST-01-C1-M 库存")
        chat_store.append_message(session, sid1, "assistant", "可用库存 693", [{"tool": "get_inventory"}])

    with session_scope() as session:
        assert chat_store.get_session(session, sid1, "admin") is not None
        assert chat_store.get_session(session, sid1, "worker1") is None
        history = chat_store.get_history(session, sid1)
        assert [m["role"] for m in history] == ["user", "assistant"]
        assert history[1]["tool_trace"]
        sessions = chat_store.list_sessions(session, "worker1")
        assert sid1 not in [s.id for s in sessions]
        assert sid2 in [s.id for s in sessions]


def test_reset_session(seeded):
    with session_scope() as session:
        s = chat_store.create_session(session, "admin")
        sid = s.id
        chat_store.append_message(session, sid, "user", "hi")
        chat_store.reset_session(session, sid)
    with session_scope() as session:
        assert chat_store.get_history(session, sid) == []


def test_history_truncation_starts_with_user_turn(seeded):
    """复审 N5：条数截断不能把一对问答切开——历史必须以用户消息起始。"""
    with session_scope() as session:
        s = chat_store.create_session(session, "admin")
        sid = s.id
        for i in range(7):                       # 13 条：user/assistant 交替，首条 user
            chat_store.append_message(session, sid, "user", f"问题 {i}")
            if i < 6:
                chat_store.append_message(session, sid, "assistant", f"回答 {i}")

    with session_scope() as session:
        history = chat_store.get_history(session, sid, limit=12)
    assert len(history) == 11, [m["role"] for m in history]
    assert history[0]["role"] == "user"
    assert history[-1]["role"] == "user"        # 末尾是本轮刚问的那句


def test_history_even_count_not_truncated(seeded):
    """偶数条且本来就以 user 开头时不误删。"""
    with session_scope() as session:
        s = chat_store.create_session(session, "admin")
        sid = s.id
        for i in range(6):
            chat_store.append_message(session, sid, "user", f"问题 {i}")
            chat_store.append_message(session, sid, "assistant", f"回答 {i}")

    with session_scope() as session:
        history = chat_store.get_history(session, sid, limit=12)
    assert len(history) == 12
    assert history[0]["role"] == "user"


def test_chat_endpoints_require_login(app_client):
    assert app_client.post("/api/chat", json={"message": "hi"}).status_code == 401
    assert app_client.get("/api/chat/sessions").status_code == 401
    assert app_client.get("/api/actions/pending").status_code == 401
    assert app_client.post("/api/actions/demo/approve", json={}).status_code == 401

def test_chat_session_owner_isolated(app_client, login, monkeypatch):
    """跨账号复用会话应被拒绝；新会话可正常创建（前端会自动恢复）。"""
    import app.api.chat_routes as chat_routes

    async def fake_run_chat(user, history, message):
        return {"reply": "ok", "tool_trace": [], "metadata_version": 1}

    monkeypatch.setattr(chat_routes, "run_chat", fake_run_chat)
    monkeypatch.setattr(chat_routes, "run_chat_stream", fake_run_chat)

    admin = login("admin", "hhw123")
    worker = login("worker2", "hhw222")

    r = app_client.post("/api/chat", headers=admin, json={"message": "hi"})
    assert r.status_code == 200
    admin_session = r.json()["session_id"]

    # 女装用户使用管理员会话 -> 拒绝（这是"会话不存在或无权访问"的来源）
    r2 = app_client.post("/api/chat", headers=worker,
                         json={"session_id": admin_session, "message": "hi"})
    assert r2.status_code == 404
    assert "无权访问" in r2.text or "会话不存在" in r2.text

    # 不带 session_id 时会创建属于自己的新会话
    r3 = app_client.post("/api/chat", headers=worker, json={"message": "hi"})
    assert r3.status_code == 200
    assert r3.json()["session_id"] != admin_session
