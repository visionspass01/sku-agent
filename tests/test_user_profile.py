"""用户偏好画像（L1 长期记忆）：读写、白名单、隔离、注入系统提示。"""

from __future__ import annotations


def test_profile_roundtrip(app_client, login):
    admin = login("admin", "hhw123")
    empty = app_client.get("/api/memory/profile", headers=admin)
    assert empty.status_code == 200
    assert empty.json()["prefs"] == {}
    assert empty.json()["watch_skus"] == []

    saved = app_client.put("/api/memory/profile", headers=admin, json={
        "prefs": {"output_style": "表格优先、结论先行", "time_window": "近 7 天",
                  "metric_pref": "可用库存（治理口径）"},
        "watch_skus": ["ST-01-C1-M", "ST-02-C1-M"],
    })
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["prefs"]["time_window"] == "近 7 天"
    assert body["watch_skus"] == ["ST-01-C1-M", "ST-02-C1-M"]
    assert body["updated_at"]

    again = app_client.get("/api/memory/profile", headers=admin).json()
    assert again["prefs"]["output_style"] == "表格优先、结论先行"


def test_profile_whitelist_filters_unknown_fields(app_client, login):
    admin = login("admin", "hhw123")
    app_client.put("/api/memory/profile", headers=admin, json={
        "prefs": {"output_style": "简洁", "hack": "rm -rf /", "note": "不要写进提示"},
        "watch_skus": ["ST-01-C1-M"],
    })
    data = app_client.get("/api/memory/profile", headers=admin).json()
    assert "hack" not in data["prefs"], data["prefs"]
    assert data["prefs"]["output_style"] == "简洁"


def test_profile_is_per_account(app_client, login):
    admin = login("admin", "hhw123")
    worker = login("worker1", "hhw111")
    app_client.put("/api/memory/profile", headers=admin, json={
        "prefs": {"output_style": "管理员偏好"}, "watch_skus": ["ST-01-C1-M"],
    })
    worker_view = app_client.get("/api/memory/profile", headers=worker).json()
    assert worker_view["prefs"] == {}, "不同账号的画像必须隔离"
    assert worker_view["watch_skus"] == []


def test_profile_requires_login(app_client):
    assert app_client.get("/api/memory/profile").status_code == 401
    assert app_client.put("/api/memory/profile", json={"prefs": {}}).status_code == 401


def test_profile_text_renders_into_prompt(app_client, login):
    from app.agents.chat_agent import build_memory_sections
    from app.db.engine import session_scope
    from app.memory import user_profile

    admin = login("admin", "hhw123")
    app_client.put("/api/memory/profile", headers=admin, json={
        "prefs": {"output_style": "表格优先", "time_window": "近 7 天"},
        "watch_skus": ["ST-01-C1-M"],
    })
    with session_scope() as session:
        text = user_profile.profile_text(session, "admin")
        assert "表格优先" in text and "ST-01-C1-M" in text
        assert len(text) <= 200
    sections = build_memory_sections(profile_text=text)
    assert "用户偏好（长期画像" in sections


def test_profile_text_is_empty_for_new_user(seeded):
    from app.db.engine import session_scope
    from app.memory import user_profile

    with session_scope() as session:
        assert user_profile.profile_text(session, "nobody") == ""
