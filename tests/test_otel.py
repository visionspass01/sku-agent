"""OTel 钩子测试：默认 no-op、配置后可导出 span、失败不影响业务。"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset():
    from app.observability import otel

    otel.reset_otel()
    yield
    otel.reset_otel()


def test_disabled_by_default(_settings):
    from app.observability import otel

    assert otel.enabled() is False
    status = otel.otel_status()
    assert status["enabled"] is False
    with otel.span("x", a=1) as current:
        assert current is None          # 未启用时拿到 None，调用方不用判空分支


def test_enabled_without_sdk_is_noop(_settings, monkeypatch):
    """配了 endpoint 但没装 SDK：只警告，不抛错（不能因为观测把业务搞挂）。"""
    from app.observability import otel

    monkeypatch.setattr(_settings, "otel_exporter_otlp_endpoint", "http://127.0.0.1:4318")
    otel.reset_otel()
    assert otel.enabled() is True
    with otel.span("tool.get_inventory", tool="get_inventory") as current:
        assert current is None
    status = otel.otel_status()
    assert status["endpoint"].startswith("http://127.0.0.1:4318")
    assert status["sdk_available"] in (True, False)   # 反映真实环境，不假设


def test_span_sets_attributes_with_fake_tracer(_settings, monkeypatch):
    """注入假 tracer：验证 span 名字与属性带上 run_detail 同源字段。"""
    from app.observability import otel

    recorded: list[dict] = []

    class _Span:
        def __init__(self, name):
            self.name = name
            self.attrs: dict = {}
            recorded.append({"name": name, "attrs": self.attrs})

        def set_attribute(self, key, value):
            self.attrs[key] = value

    class _Tracer:
        def start_as_current_span(self, name):
            span = _Span(name)

            class _CM:
                def __enter__(self_inner):
                    return span

                def __exit__(self_inner, *exc):
                    return False

            return _CM()

    monkeypatch.setattr(otel, "_get_tracer", lambda: _Tracer())
    with otel.span("chat.turn", turn_id="t_1", session_id="s_1", empty=""):
        pass
    assert recorded[0]["name"] == "chat.turn"
    assert recorded[0]["attrs"] == {"sku_agent.turn_id": "t_1", "sku_agent.session_id": "s_1"}


def test_health_exposes_observability(app_client):
    body = app_client.get("/api/health").json()
    assert body["observability"]["enabled"] is False
    assert "note" in body["observability"]


def test_tool_contract_survives_otel_failure(seeded, monkeypatch):
    """观测埋点抛错时，工具调用必须照常返回结果（这是硬要求）。"""
    from app.memory.tool_contract import with_result_contract

    def _boom(*args, **kwargs):
        raise RuntimeError("otel down")

    monkeypatch.setattr("app.observability.otel.span", _boom)

    async def _tool(sku_id: str) -> dict:
        return {"available": 10}

    wrapped = with_result_contract("get_inventory")(_tool)
    import asyncio

    envelope = asyncio.run(wrapped(sku_id="ST-01-C1-M"))
    assert envelope.get("available") == 10
    assert "result_id" in envelope
