"""多 Agent 注册 / 发现（评审 C2 / 2.1-2）。

验收的是"插拔"到底成不成立：
- 注册后**立刻**能用（不重启中台）；
- 改 scope / 轮换令牌 / 禁用都即时生效；
- 列表永不泄露令牌，明文只出现一次；
- 非 admin 不能注册（否则任何 Agent 都能给自己提权）。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

LEGACY = {"X-Internal-Token": "demo-internal-token"}     # 全权主体（含 admin）
MEN_SKU = "ST-05-C1-M"
WOMEN_SKU = "ST-01-C1-M"


@pytest.fixture(scope="module")
def platform_db(tmp_path_factory):
    import datetime as dt

    from data_platform.api import cache as pcache
    from data_platform.db import init_schema, make_engine, set_engine
    from data_platform.ingest.airflow_mock import run_dag
    from data_platform.mock_sources import generate_all

    tmp = tmp_path_factory.mktemp("platform_agents")
    engine = make_engine(f"sqlite:///{(tmp / 'platform.db').as_posix()}")
    set_engine(engine)
    init_schema(engine)
    pcache.reset_client()
    generate_all(snapshot_date=dt.date(2026, 11, 3), seed=7)
    result = run_dag("daily_sync")
    assert all(t["status"] == "success" for t in result["tasks"].values()), result
    yield engine
    set_engine(None)


@pytest.fixture(autouse=True)
def _principals(monkeypatch):
    """环境变量身份：一个只读 Agent + 一个非 admin 的写 Agent（用来验证注册需要 admin）。"""
    from data_platform import config
    from data_platform.api import authz, quota

    keys = {
        "reader": {"key": "k-reader", "scopes": ["read"], "departments": None},
        "writer": {"key": "k-writer", "scopes": ["read", "write"], "departments": None},
    }
    monkeypatch.setattr(config, "API_KEYS_RAW", json.dumps(keys, ensure_ascii=False))
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MINUTE", 600)
    authz.reset_principals()
    quota.reset()
    yield
    authz.reset_principals()
    quota.reset()


@pytest.fixture()
def client(platform_db):
    from data_platform.api.main import app

    with TestClient(app) as c:
        yield c
    # 清理注册表，避免用例间互相污染（同一个临时库）
    from data_platform.db import PlatformAgent, session_scope

    with session_scope() as session:
        for row in session.query(PlatformAgent).all():
            session.delete(row)


def _register(client, agent_id: str, **spec) -> dict:
    body = {"agent_id": agent_id, "name": agent_id, **spec}
    resp = client.post("/api/v1/agents", headers=LEGACY, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_register_then_use_immediately(client):
    created = _register(client, "image-agent", scopes=["read"], departments=None)
    assert created["token"].startswith("k-")
    assert created["agent_id"] == "image-agent"

    headers = {"X-Internal-Token": created["token"]}
    resp = client.get(f"/api/v1/skus/{MEN_SKU}/metrics", headers=headers)
    assert resp.status_code == 200, resp.text
    # 身份是真的被中台认出来的，不是靠默认主体
    assert resp.headers["X-Agent-Id"] == "image-agent"

    # 审计里能按 agent_id 查到调用（治理覆盖到新 Agent）
    audit = client.get("/api/v1/audit?agent_id=image-agent", headers=LEGACY).json()
    assert audit["count"] >= 1
    assert all(item["agent_id"] == "image-agent" for item in audit["items"])


def test_token_is_never_listed(client):
    created = _register(client, "gen-agent", scopes=["read"])
    listed = client.get("/api/v1/agents", headers=LEGACY).json()
    assert listed["count"] >= 1
    blob = json.dumps(listed, ensure_ascii=False)
    assert created["token"] not in blob, "列表接口泄露了明文令牌"
    assert all("token" not in item for item in listed["items"])


def test_duplicate_registration_is_409(client):
    _register(client, "dup-agent", scopes=["read"])
    resp = client.post("/api/v1/agents", headers=LEGACY,
                       json={"agent_id": "dup-agent", "scopes": ["read"]})
    assert resp.status_code == 409


def test_registration_requires_admin(client):
    resp = client.post("/api/v1/agents", headers={"X-Internal-Token": "k-writer"},
                       json={"agent_id": "self-promote", "scopes": ["read", "write"]})
    assert resp.status_code == 403
    assert "scope=admin" in resp.json()["detail"]


def test_patch_scope_takes_effect_immediately(client):
    created = _register(client, "scope-agent", scopes=["read"])
    headers = {"X-Internal-Token": created["token"]}
    assert client.get("/api/v1/etl/runs", headers=headers).status_code == 403

    patched = client.patch("/api/v1/agents/scope-agent", headers=LEGACY,
                           json={"scopes": ["read", "etl"]})
    assert patched.status_code == 200
    assert sorted(patched.json()["scopes"]) == ["etl", "read"]
    assert client.get("/api/v1/etl/runs", headers=headers).status_code == 200


def test_department_scope_applies_to_registered_agent(client):
    created = _register(client, "mens-agent", scopes=["read"], departments=["男装"])
    headers = {"X-Internal-Token": created["token"]}
    assert client.get(f"/api/v1/skus/{MEN_SKU}/metrics", headers=headers).status_code == 200
    cross = client.get(f"/api/v1/skus/{WOMEN_SKU}/metrics", headers=headers)
    assert cross.status_code == 403
    assert "只能访问" in cross.json()["detail"]

    # 目录接口也按部门过滤：Agent 侧作用域判定拿到的就是可见集合
    catalog_body = client.get("/api/v1/metadata/catalog", headers=headers).json()
    assert catalog_body["scope_filtered"] is True
    assert catalog_body["scope"] == ["男装"]


def test_rotate_invalidates_old_token(client):
    created = _register(client, "rotate-agent", scopes=["read"])
    old = {"X-Internal-Token": created["token"]}
    assert client.get(f"/api/v1/skus/{MEN_SKU}/metrics", headers=old).status_code == 200

    rotated = client.post("/api/v1/agents/rotate-agent/rotate", headers=LEGACY).json()
    assert rotated["token"] != created["token"]
    assert client.get(f"/api/v1/skus/{MEN_SKU}/metrics", headers=old).status_code == 401
    new = {"X-Internal-Token": rotated["token"]}
    assert client.get(f"/api/v1/skus/{MEN_SKU}/metrics", headers=new).status_code == 200


def test_disable_blocks_calls(client):
    created = _register(client, "temp-agent", scopes=["read"])
    headers = {"X-Internal-Token": created["token"]}
    assert client.get(f"/api/v1/skus/{MEN_SKU}/metrics", headers=headers).status_code == 200

    disabled = client.post("/api/v1/agents/temp-agent/disable", headers=LEGACY)
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert client.get(f"/api/v1/skus/{MEN_SKU}/metrics", headers=headers).status_code == 401


def test_me_reports_identity_and_scopes(client):
    created = _register(client, "self-check", scopes=["read"], departments=["女装"],
                        rate_limit=42)
    me = client.get("/api/v1/agents/me",
                    headers={"X-Internal-Token": created["token"]}).json()
    assert me["agent_id"] == "self-check"
    assert sorted(me["scopes"]) == ["read"]
    assert me["departments"] == ["女装"]
    assert me["rate_limit"] == 42
    assert me["source"] == "db"
    # 环境变量身份也能 /me（老部署不必先注册）
    legacy_me = client.get("/api/v1/agents/me", headers=LEGACY).json()
    assert legacy_me["agent_id"] == "legacy-internal"
    assert "admin" in legacy_me["scopes"]


def test_invalid_spec_is_rejected(client):
    bad_scope = client.post("/api/v1/agents", headers=LEGACY,
                            json={"agent_id": "x1", "scopes": ["superuser"]})
    assert bad_scope.status_code == 400 and "未知 scope" in bad_scope.json()["detail"]

    bad_id = client.post("/api/v1/agents", headers=LEGACY,
                         json={"agent_id": "Bad ID!", "scopes": ["read"]})
    assert bad_id.status_code == 400

    bad_dept = client.post("/api/v1/agents", headers=LEGACY,
                           json={"agent_id": "x2", "scopes": ["read"], "departments": ["男装部"]})
    assert bad_dept.status_code == 400 and "未知部门" in bad_dept.json()["detail"]
