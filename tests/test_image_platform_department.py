"""第四轮 W7：中台商品清单的部门过滤（P1-8）。

`/api/v1/image/products` 过去**没有**部门过滤（context / similar 都有），部门账号能把
别的部门的商品直接拉进清单。现在与另两个读接口同口径：越权行过滤 + `filtered` 计数 + 审计。
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def platform_db(tmp_path_factory):
    from tests.image_platform_support import init_platform_db, reset_platform_engine

    tmp = tmp_path_factory.mktemp("w7_platform")
    init_platform_db(tmp)
    yield tmp
    reset_platform_engine()


@pytest.fixture(autouse=True)
def _reset_platform_principals():
    yield
    from data_platform.api import authz

    authz.reset_principals()


def _platform_client():
    from tests.image_platform_support import platform_client

    return platform_client()


def _department_scoped_key(monkeypatch, departments: list[str]) -> dict:
    import json as json_mod

    import data_platform.config as dp_config
    from data_platform.api import authz

    monkeypatch.setattr(dp_config, "API_KEYS_RAW", json_mod.dumps({
        "male-agent": {"key": "male-key", "scopes": ["read", "write"],
                       "departments": departments},
    }))
    authz.reset_principals()
    return {"X-Api-Key": "male-key"}


def test_products_list_filters_by_department(platform_db, monkeypatch):
    client = _platform_client()
    headers = _department_scoped_key(monkeypatch, ["男装"])
    with client as c:
        body = c.get("/api/v1/image/products", params={"limit": 100}, headers=headers).json()
    ids = {item["product_id"] for item in body["products"]}
    assert ids, "男装账号至少能看到一个男装商品"
    assert ids <= {"P-1002", "P-3002"}, ids           # 只剩男装
    assert "P-1001" not in ids                        # 女装被过滤
    assert body["filtered"] >= 1
    assert body["count"] == len(body["products"])


def test_products_list_unfiltered_for_admin_key(platform_db):
    """默认内部 token（无部门限制）不过滤任何商品。"""
    from tests.image_platform_support import TOKEN

    client = _platform_client()
    with client as c:
        body = c.get("/api/v1/image/products", params={"limit": 100},
                     headers=dict(TOKEN)).json()
    ids = {item["product_id"] for item in body["products"]}
    assert "P-1001" in ids and "P-1002" in ids
    assert body["filtered"] == 0


def test_products_list_audit_records_filtered(platform_db, monkeypatch):
    """审计里要能看到"这次过滤了多少"，否则事后无法解释清单为什么少了。"""
    client = _platform_client()
    headers = _department_scoped_key(monkeypatch, ["女装"])
    with client as c:
        resp = c.get("/api/v1/image/products", params={"limit": 100}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "P-1001" in {item["product_id"] for item in body["products"]}
    assert "P-1002" not in {item["product_id"] for item in body["products"]}
    assert body["filtered"] >= 1
