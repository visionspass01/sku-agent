"""工具层部门作用域（A1）：统一声明 + 统一守卫，覆盖「非 SKU 入参」的工具。

回归背景：作用域校验原来写在每个工具函数体内（`_scope_error(user, sku_id)`），
于是**入参里没有 sku_id 的工具完全不受保护**：
`list_pending_approvals` 把别的部门的待审批单返回给部门账号，
`get_sales_rate` 的 department 由模型随便填。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

ITEMS = [
    {"approval_id": "appr-men", "sku_id": "ST-92-C1-M", "qty": 500, "amount": 50000,
     "status": "pending_confirmation"},
    {"approval_id": "appr-women", "sku_id": "ST-90-C1-M", "qty": 800, "amount": 80000,
     "status": "pending_confirmation"},
]


@pytest.fixture()
def platform_client(monkeypatch):
    """中台桩：/approvals 不按部门过滤（内部 token 调用），事件都把请求参数记下来。"""
    from app.tools import platform_tools

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path,
                     "department": request.url.params.get("department")})
        if request.url.path.endswith("/approvals"):
            return httpx.Response(200, json={"status": "pending", "items": ITEMS})
        if request.url.path.endswith("/sales-rate"):
            dept = request.url.params.get("department")
            return httpx.Response(200, json={"department": dept, "sales_rate": 0.41})
        if request.url.path.endswith("/inventory"):
            return httpx.Response(200, json={"sku_id": "ST-92-C1-M", "available": 510})
        return httpx.Response(404, json={"detail": "not found"})

    client = platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler),
    )
    platform_tools.set_platform_client(client)
    yield seen
    platform_tools.set_platform_client(None)


def _user(username: str, department: str | None, role: str = "user"):
    from app.auth import AuthUser

    return AuthUser(username=username, role=role, department=department)


def _tools(user) -> dict:
    from app.tools.platform_tools import build_platform_tools

    return {tool.name: tool for tool in build_platform_tools(user)}


def test_pending_approvals_are_scope_filtered(seeded, platform_client):
    """男装账号只能看到男装 SKU 的待审批单（修复前会拿到女装那条）。"""
    worker = _user("worker1", "男装")
    data = asyncio.run(_tools(worker)["list_pending_approvals"].ainvoke({}))

    assert [i["approval_id"] for i in data["items"]] == ["appr-men"]
    assert data["scope_filtered"] is True
    assert data["scope_dropped"] == 1
    assert data["count"] == 1
    assert data["result_id"].startswith("res_")   # 契约仍生效


def test_pending_approvals_admin_sees_all(seeded, platform_client):
    admin = _user("admin", None, role="admin")
    data = asyncio.run(_tools(admin)["list_pending_approvals"].ainvoke({}))

    assert {i["approval_id"] for i in data["items"]} == {"appr-men", "appr-women"}
    assert data["scope_filtered"] is False
    assert data["scope_dropped"] == 0


def test_sales_rate_department_is_forced_for_department_user(seeded, platform_client):
    """模型填别的部门也不生效：部门账号的 department 被强制回本部门。"""
    worker = _user("worker1", "男装")
    data = asyncio.run(_tools(worker)["get_sales_rate"].ainvoke({"department": "女装"}))

    assert platform_client[-1]["department"] == "男装"
    assert data["department"] == "男装"
    assert data["department_forced"] is True


def test_sales_rate_department_passthrough_for_admin(seeded, platform_client):
    admin = _user("admin", None, role="admin")
    data = asyncio.run(_tools(admin)["get_sales_rate"].ainvoke({"department": "女装"}))

    assert platform_client[-1]["department"] == "女装"
    assert "department_forced" not in data


def test_sku_tools_still_deny_cross_department(seeded, platform_client):
    """原有的 SKU 级校验迁移到守卫后行为不变（含错误文案与 business 归类）。"""
    worker = _user("worker1", "男装")
    result = asyncio.run(_tools(worker)["get_inventory"].ainvoke({"sku_id": "ST-90-C1-M"}))
    assert "无权限" in result["error"]
    assert result.get("error_kind") == "business"

    own = asyncio.run(_tools(worker)["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
    assert "error" not in own


def test_order_tools_deny_cross_department_order(seeded, platform_client):
    """订单类工具的校验也走声明（order_id → SKU → 部门）。"""
    import datetime as dt

    from app.db.engine import session_scope
    from app.db.models import MockOrder

    with session_scope() as session:
        session.add(MockOrder(order_id="ORD-W1", sku_id="ST-90-C1-M", qty=1, amount=299.0,
                             status="已发货", channel="淘系", created_at=dt.datetime(2026, 11, 1, 9)))
    worker = _user("worker1", "男装")
    result = asyncio.run(_tools(worker)["get_order_status"].ainvoke({"order_id": "ORD-W1"}))
    assert "无权限" in result["error"]


def test_every_tool_declares_a_scope():
    """声明表与工具清单必须完全对齐（新增工具忘记声明 → 这里红）。"""
    from app.security.scope import SCOPE_KINDS
    from app.tools.platform_tools import ALL_TOOL_NAMES, TOOL_SCOPES

    assert set(TOOL_SCOPES) == ALL_TOOL_NAMES
    assert set(TOOL_SCOPES.values()) <= SCOPE_KINDS


def test_missing_scope_declaration_fails_loudly(monkeypatch):
    """漏声明作用域时装配直接失败，而不是「少一层校验、静默放行」。"""
    from app.tools import platform_tools

    monkeypatch.delitem(platform_tools.TOOL_SCOPES, "get_inventory")
    with pytest.raises(RuntimeError, match="未在 TOOL_SCOPES"):
        platform_tools.build_platform_tools(_user("admin", None, role="admin"))
    monkeypatch.setitem(platform_tools.TOOL_SCOPES, "get_inventory", "sku")


def test_tool_catalogue_exposes_scopes():
    from app.tools.platform_tools import ALL_TOOL_NAMES, tool_catalogue

    catalogue = tool_catalogue()
    assert set(catalogue["scopes"]) == ALL_TOOL_NAMES
    assert json.dumps(catalogue["scopes"], ensure_ascii=False)  # 可序列化（给 /api/agents 用）
