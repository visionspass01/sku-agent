"""补货单幂等键（复审 A2）：模型不能通过入参影响幂等键。

背景：早先 `create_replenishment_order(request_id=...)` 会把模型传入的 `request_id`
拼进幂等键 —— 而提示词又要求模型"必须带幂等信息"，等于**主动诱发**重复建单：
同一请求重放两次得到两个不同的键，中台侧据此建出两张草稿单。
现在该形参被删除，键固定为「会话 + 轮次 + SKU + 数量 + 原因」。
"""

from __future__ import annotations

import asyncio
import inspect
import json

import httpx
import pytest


def _platform(monkeypatch):
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/erp/replenish-orders"):
            body = json.loads(request.content.decode("utf-8"))
            posted.append(body)
            return httpx.Response(200, json={
                "status": "pending_confirmation", "po_no": f"PO-{len(posted)}",
                "approval_id": f"appr-{len(posted)}", "qty": body.get("qty"),
            })
        return httpx.Response(404, json={"detail": "not found"})

    from app.tools import platform_tools

    platform_tools.set_platform_client(platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler),
    ))
    monkeypatch.setattr(platform_tools, "build_platform_tools", platform_tools.build_platform_tools)
    return posted


@pytest.fixture()
def posted(monkeypatch):
    captured = _platform(monkeypatch)
    yield captured
    from app.tools import platform_tools

    platform_tools.set_platform_client(None)


def _tool():
    from app.auth import AuthUser
    from app.tools.platform_tools import build_platform_tools

    tools = build_platform_tools(AuthUser(username="admin", role="admin", department=None),
                                 allow=["create_replenishment_order"])
    return {t.name: t for t in tools}["create_replenishment_order"]


def _call(tool, **args):
    from app.memory.runtime import tool_context, turn_log_context

    async def _run():
        with tool_context(session_id="s-idem", username="admin", department=None,
                          agent_id="operator"):
            with turn_log_context("t_idem", "s-idem"):
                return await tool.ainvoke(args)

    return asyncio.run(_run())


def test_signature_has_no_model_controlled_idempotency_key():
    """幂等键的输入必须全是业务事实（SKU/数量/原因），模型不能自带键。"""
    params = inspect.signature(_tool().coroutine).parameters
    assert "request_id" not in params
    assert "idempotency_key" not in params
    assert set(params) == {"sku_id", "qty", "reason"}


def test_same_arguments_reuse_the_same_key(posted):
    tool = _tool()
    _call(tool, sku_id="ST-01-C1-M", qty=120, reason="断码补货")
    _call(tool, sku_id="ST-01-C1-M", qty=120, reason="断码补货")

    assert len(posted) == 2
    assert posted[0]["idempotency_key"] == posted[1]["idempotency_key"]


def test_different_arguments_get_different_keys(posted):
    tool = _tool()
    _call(tool, sku_id="ST-01-C1-M", qty=120, reason="断码补货")
    _call(tool, sku_id="ST-01-C1-M", qty=150, reason="断码补货")
    _call(tool, sku_id="ST-01-C1-M", qty=120, reason="大促备货")

    keys = [body["idempotency_key"] for body in posted]
    assert len(set(keys)) == 3


def test_model_supplied_token_cannot_change_the_key(posted):
    """即便模型硬塞一个"幂等键"，args_schema 也会丢弃它（extra=ignore），键保持稳定。"""
    tool = _tool()
    _call(tool, sku_id="ST-01-C1-M", qty=120, reason="断码补货")
    _call(tool, sku_id="ST-01-C1-M", qty=120, reason="断码补货", request_id="invented-1")
    _call(tool, sku_id="ST-01-C1-M", qty=120, reason="断码补货", idempotency_key="invented-2")

    keys = [body["idempotency_key"] for body in posted]
    assert len(set(keys)) == 1, keys
    assert all("request_id" not in body for body in posted)
    assert all("invented" not in json.dumps(body, ensure_ascii=False) for body in posted)


def test_script_call_without_context_does_not_dedupe(posted):
    """没有会话/轮次上下文（脚本直调）时不去重：否则两次独立脚本会被误合并成一张单。"""
    tool = _tool()

    async def _run():
        return await tool.ainvoke({"sku_id": "ST-01-C1-M", "qty": 120, "reason": "脚本补货"})

    asyncio.run(_run())
    asyncio.run(_run())
    keys = [body["idempotency_key"] for body in posted]
    assert len(set(keys)) == 2
