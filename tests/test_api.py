"""FastAPI 接口测试（TestClient + FakeLLM，不依赖网络）。

所有数据接口都需要登录：管理员 admin / 男装部门 worker1 / 女装部门 worker2。
"""

from __future__ import annotations

from fakes import FakeLLM


def test_health_public(app_client):
    data = app_client.get("/api/health").json()
    assert data["status"] == "ok"
    assert data["mcp_transport"] == "direct"
    assert data["scheduler_enabled"] is False


def test_health_reports_kb_conflict_observability(app_client):
    """口径冲突机制必须可观测：计数 + 语料覆盖率（否则失败模式全是静默的）。"""
    data = app_client.get("/api/health").json()
    conflicts = data["rag_conflicts"]
    assert {"checked", "ok", "none", "unavailable", "intra"} <= set(conflicts)
    facts = data["rag_facts"]
    assert facts["documents"] > 0
    assert facts["facts"] > 0
    assert facts["unmatched_count"] == 0, facts["unmatched_sections"]

    metrics = app_client.get("/api/metrics").json()
    assert "conflicts" in metrics["rag"]


def test_public_reads_without_login(app_client):
    """读接口（数据展示）无需登录即可访问；匿名视角为全部门只读。"""
    for path in ("/api/metrics", "/api/skus", "/api/anomalies",
                 "/api/rag/search?q=补货", "/api/queue/stats"):
        assert app_client.get(path).status_code == 200, path
    metrics = app_client.get("/api/metrics").json()
    assert metrics["scope"]["role"] == "anonymous"
    assert "男装" in metrics["departments"]
    rag = app_client.get("/api/rag/search", params={"q": "补货"}).json()
    assert rag["count"] > 0


def test_action_endpoints_require_login(app_client):
    """写操作 / 账号接口必须登录。"""
    assert app_client.get("/api/auth/me").status_code == 401
    assert app_client.get("/api/auth/security-records").status_code == 401
    assert app_client.post("/api/patrol", json={"mode": "hourly", "limit": 1}).status_code == 401
    assert app_client.post("/api/inspect", json={"sku_id": "ST-90-C1-M"}).status_code == 401


def test_list_skus_and_detail(app_client, login):
    headers = login("admin", "hhw123")
    data = app_client.get("/api/skus", params={"limit": 10}, headers=headers).json()
    assert data["count"] > 0
    sku_id = data["items"][0]["sku_id"]
    detail = app_client.get(f"/api/skus/{sku_id}", headers=headers).json()
    assert detail["sku"]["sku_id"] == sku_id
    assert "sales_last_7d" in detail


def test_sku_not_found(app_client, login):
    headers = login("admin", "hhw123")
    resp = app_client.get("/api/skus/NO-SUCH-SKU", headers=headers)
    assert resp.status_code == 404


def test_anomalies(app_client, login):
    headers = login("admin", "hhw123")
    data = app_client.get("/api/anomalies", headers=headers).json()
    assert data["count"] > 0
    assert "reasons" in data["items"][0]


def test_rag_search(app_client, login):
    headers = login("admin", "hhw123")
    data = app_client.get("/api/rag/search", params={"q": "清仓 折扣"}, headers=headers).json()
    assert data["count"] > 0
    assert data["hits"][0]["doc_id"] in (
        "sop-clearance", "sop-clearance-women", "sop-clearance-men", "sop-season", "case-history",
    )


def test_queue_stats(app_client, login):
    headers = login("admin", "hhw123")
    data = app_client.get("/api/queue/stats", headers=headers).json()
    assert "length" in data and "pending" in data


def test_metrics_admin(app_client, login):
    headers = login("admin", "hhw123")
    data = app_client.get("/api/metrics", headers=headers).json()
    assert data["sku_count"] > 0
    assert data["scope"]["role"] == "admin"
    assert "男装" in data["departments"] and "女装" in data["departments"]
    assert "cache" in data and "queue" in data


def test_inspect_end_to_end(app_client, login):
    import app.llm as llm_mod

    llm_mod._llm = FakeLLM(passed=True)
    headers = login("admin", "hhw123")
    resp = app_client.post("/api/inspect", json={"sku_id": "ST-90-C1-M"}, headers=headers)
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    detail = app_client.get(f"/api/inspections/{run_id}", headers=headers).json()
    assert detail["kind"] == "single"
    assert detail["status"] in ("ok", "degraded")
    assert detail["results"]
    result = detail["results"][0]
    assert result["sku_id"] == "ST-90-C1-M"
    assert result["verifier"]["passed"] is True
    assert len(result["plan"]["citations"]) >= 2
    assert result["data_bundle"]["trace"]
    # 引用溯源只应来自「女装 + 通用」SOP
    departments = {c.get("department") for c in result["data_bundle"].get("rag_citations") or []}
    assert departments <= {"女装", "通用"}, departments
