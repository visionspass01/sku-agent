"""认证、权限与数据隔离测试。"""

from __future__ import annotations


def test_login_success_returns_token_and_identity(app_client):
    resp = app_client.post("/api/auth/login", json={"username": "admin", "password": "hhw123"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["token"]
    assert data["user"] == {"username": "admin", "role": "admin", "department": None}

    me = app_client.get("/api/auth/me", headers={"Authorization": f"Bearer {data['token']}"})
    assert me.status_code == 200
    assert me.json()["user"]["role"] == "admin"


def test_login_wrong_password(app_client):
    resp = app_client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    assert resp.status_code == 401


def test_invalid_token_rejected(app_client):
    # 读接口对无效 Token 按匿名只读处理；账号接口必须拒绝非法 Token
    assert app_client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"}).status_code == 401
    assert app_client.get("/api/metrics", headers={"Authorization": "Bearer not-a-real-token"}).status_code == 200


def test_user_only_sees_own_department_skus(app_client, login):
    headers = login("worker1", "hhw111")
    data = app_client.get("/api/skus", params={"limit": 200}, headers=headers).json()
    assert data["count"] > 0
    assert {item["category"] for item in data["items"]} == {"男装"}

    # 女装 SKU 对男装用户不可见（404 防探测）
    assert app_client.get("/api/skus/ST-90-C1-M", headers=headers).status_code == 404
    assert app_client.post("/api/inspect", json={"sku_id": "ST-90-C1-M"}, headers=headers).status_code == 404


def test_worker2_only_sees_women_department(app_client, login):
    headers = login("worker2", "hhw222")
    data = app_client.get("/api/skus", params={"limit": 200}, headers=headers).json()
    categories = {item["category"] for item in data["items"]}
    assert categories <= {"女装", "童装"}
    assert app_client.get("/api/skus/ST-92-C1-M", headers=headers).status_code == 404


def test_metrics_scoped_by_role(app_client, login):
    admin = app_client.get("/api/metrics", headers=login("admin", "hhw123")).json()
    assert admin["scope"]["role"] == "admin"
    assert set(admin["departments"]) == {"男装", "女装"}
    assert admin["departments"]["男装"]["sku_count"] >= 1
    assert admin["departments"]["女装"]["sku_count"] >= 1

    worker = app_client.get("/api/metrics", headers=login("worker1", "hhw111")).json()
    assert worker["scope"]["role"] == "user"
    assert worker["scope"]["department"] == "男装"
    assert list(worker["departments"]) == ["男装"]
    assert worker["sku_count"] == worker["departments"]["男装"]["sku_count"]


def test_anomalies_scoped_by_department(app_client, login):
    w1 = app_client.get("/api/anomalies", headers=login("worker1", "hhw111")).json()
    assert w1["count"] >= 1
    assert all(item["sku_id"].startswith("ST-92") for item in w1["items"])

    w2 = app_client.get("/api/anomalies", headers=login("worker2", "hhw222")).json()
    assert w2["count"] >= 1
    assert all(not item["sku_id"].startswith("ST-92") for item in w2["items"])


def test_rag_respects_department_scope(app_client, login):
    worker = app_client.get(
        "/api/rag/search", params={"q": "女装 连衣裙 清仓 折扣"}, headers=login("worker1", "hhw111")
    ).json()
    assert worker["count"] > 0
    assert all(h["meta"]["department"] in ("男装", "通用") for h in worker["hits"]), worker["hits"]

    admin = app_client.get(
        "/api/rag/search", params={"q": "女装 连衣裙 清仓 折扣"}, headers=login("admin", "hhw123")
    ).json()
    assert any(h["meta"]["department"] == "女装" for h in admin["hits"])


def test_security_records(app_client, login):
    headers = login("admin", "hhw123")
    data = app_client.get("/api/auth/security-records", headers=headers).json()
    assert data["window_hours"] == 4
    assert "4 小时" in data["note"]
    assert data["records"]
    first = data["records"][0]
    assert set(first) == {"username", "ip", "at", "result"}
    assert first["ip"]
    assert first["result"] == "ok"

    # 部门用户只能看到自己的登录记录
    worker = app_client.get("/api/auth/security-records", headers=login("worker1", "hhw111")).json()
    assert worker["records"]
    assert {r["username"] for r in worker["records"]} == {"worker1"}


def test_logout_revokes_token(app_client, login):
    headers = login("worker1", "hhw111")
    assert app_client.get("/api/auth/me", headers=headers).status_code == 200
    assert app_client.post("/api/auth/logout", headers=headers).status_code == 200
    assert app_client.get("/api/auth/me", headers=headers).status_code == 401


def test_login_rate_limited_after_repeated_failures(app_client, _settings, monkeypatch):
    """失败达到上限 → 429 且带 Retry-After；窗口过去后自动恢复；成功会清零计数。"""
    import time

    from app.auth import reset_login_throttle

    reset_login_throttle()
    monkeypatch.setattr(_settings, "login_rate_limit", 3)
    monkeypatch.setattr(_settings, "login_rate_window_seconds", 0.4)
    try:
        for _ in range(3):
            resp = app_client.post("/api/auth/login",
                                   json={"username": "worker2", "password": "wrong"})
            assert resp.status_code == 401

        blocked = app_client.post("/api/auth/login",
                                  json={"username": "worker2", "password": "hhw222"})
        assert blocked.status_code == 429, blocked.text
        assert "频繁" in blocked.json()["detail"]
        assert blocked.headers.get("Retry-After")

        time.sleep(0.5)   # 窗口过去：即使密码正确也重新可用
        ok = app_client.post("/api/auth/login",
                             json={"username": "worker2", "password": "hhw222"})
        assert ok.status_code == 200

        # 成功登录清零失败计数：再错 2 次仍不会被限流
        for _ in range(2):
            assert app_client.post("/api/auth/login",
                                   json={"username": "worker2", "password": "bad"}).status_code == 401
        assert app_client.post("/api/auth/login",
                               json={"username": "worker2", "password": "hhw222"}).status_code == 200
    finally:
        reset_login_throttle()


def test_failed_login_recorded_as_security_event(app_client, login):
    """失败尝试也要留痕：安全记录里能看到 result=failed，便于排查爆破。"""
    app_client.post("/api/auth/login", json={"username": "worker1", "password": "nope"})
    headers = login("admin", "hhw123")
    data = app_client.get("/api/auth/security-records", headers=headers).json()
    failed = [r for r in data["records"] if r["username"] == "worker1" and r["result"] == "failed"]
    assert failed, data["records"][:5]
