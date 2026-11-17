"""实时进度（SSE）、延迟分位（P50/P95）、静态资源版本化 测试。"""

from __future__ import annotations

import asyncio
import json

import httpx


# ---------------------------------------------------------------- 延迟采样器
def test_latency_percentiles():
    from app.observability import latency

    for value in (10, 20, 30, 40, 50):
        latency.record("api", value)
    snap = latency.snapshot()
    api = snap["categories"]["api"]
    assert api["count"] == 5 and api["window_count"] == 5
    assert api["p50"] == 30 and api["p95"] == 50 and api["p99"] == 50
    assert api["min"] == 10 and api["max"] == 50
    overall = snap["overall"]
    assert overall["count"] == 5
    assert snap["window"] == latency.MAX_SAMPLES
    assert "进程内" in snap["note"]


def test_latency_categories_are_isolated():
    from app.observability import latency

    latency.record("tool", 100)
    latency.record("llm", 2000)
    snap = latency.snapshot()
    assert snap["categories"]["tool"]["count"] == 1
    assert snap["categories"]["llm"]["count"] == 1
    assert snap["overall"]["count"] == 2
    assert snap["overall"]["max"] == 2000


def test_metrics_exposes_latency(app_client):
    from app.observability import latency

    latency.record("api", 42)
    data = app_client.get("/api/metrics").json()
    assert "latency" in data
    assert data["latency"]["overall"]["count"] >= 1
    assert "categories" in data["latency"] and "labels" in data["latency"]


def test_tool_and_platform_latency_sampled(seeded):
    """工具调用会同时记录 tool 与 platform 两类样本。"""
    from app.auth import AuthUser
    from app.observability import latency
    from app.tools import platform_tools

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sku_id": "ST-92-C1-M", "available": 10})

    client = platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler),
    )
    platform_tools.set_platform_client(client)
    try:
        user = AuthUser(username="admin", role="admin", department=None)
        tools = {t.name: t for t in platform_tools.build_platform_tools(user)}
        asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
    finally:
        platform_tools.set_platform_client(None)

    snap = latency.snapshot()
    assert snap["categories"]["tool"]["count"] >= 1
    assert snap["categories"]["platform"]["count"] >= 1


# ---------------------------------------------------------------- 静态资源版本化
def test_index_injects_asset_versions(app_client):
    from app.api.routes import health

    resp = app_client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert '/app.js?v=' in html and '/style.css?v=' in html
    assert 'data-ui-build="' in html
    # 同一份版本号也出现在 health 里，前端自检可对比
    data = health()
    assert data["assets"]["app.js"] != "missing"
    assert "chat_stream" in data["features"] and "latency" in data["features"]


def test_index_route_also_serves_index_html(app_client):
    resp = app_client.get("/index.html")
    assert resp.status_code == 200
    assert '/app.js?v=' in resp.text


# ---------------------------------------------------------------- SSE 实时进度
def _read_sse(app_client, payload, headers) -> tuple[int, list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []
    with app_client.stream("POST", "/api/chat/stream", json=payload, headers=headers) as resp:
        status = resp.status_code
        buffer = ""
        for chunk in resp.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                event, data = "message", {}
                for line in block.splitlines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line[5:].strip() or "{}")
                events.append((event, data))
    return status, events


def test_chat_stream_emits_progress_events(app_client, login, monkeypatch):
    import app.api.chat_routes as chat_routes
    from app.memory.runtime import emit_event, emit_stage

    async def fake_run_chat(user, history, message):
        emit_stage("plan", "解析查询计划")
        emit_stage("tool", "取数：get_inventory")
        # 模型调用事件：进度卡靠它把「等了几秒」归因到模型而不是工具
        emit_event("llm_start", {"call": 1, "label": "模型调用 #1"})
        emit_event("llm_end", {"call": 1, "kind": "tool_call", "duration_ms": 1300,
                               "input_tokens": 900, "output_tokens": 40})
        return {
            "reply": "可用库存 693 件（res_aaa111bbb222）",
            "tool_trace": [],
            "metadata_version": 1,
            "query_plan": {"plan_id": "qp_sse", "intent": "lookup", "confidence": 0.8},
            "clarify": None,
            "cited_result_ids": ["res_aaa111bbb222"],
            "memory": {"facts_version": 1, "working_version": 2},
            "session_references": [],
            "run_detail": {"turn_id": "t_sse", "steps": [], "memory": {}, "llm": {}},
        }

    monkeypatch.setattr(chat_routes, "run_chat", fake_run_chat)
    monkeypatch.setattr(chat_routes, "run_chat_stream", fake_run_chat)
    admin = login("admin", "hhw123")
    status, events = _read_sse(app_client, {"message": "ST-01-C1-M 库存"}, admin)
    assert status == 200
    kinds = [e for e, _ in events]
    assert "stage" in kinds
    assert "llm_start" in kinds and "llm_end" in kinds, kinds
    done = [data for e, data in events if e == "done"]
    assert done, kinds
    payload = done[-1]
    assert payload["session_id"]
    assert payload["run_detail"]["turn_id"] == "t_sse"
    assert "features" in payload
    # stage 事件带耗时字段
    stage = [data for e, data in events if e == "stage"][0]
    assert "elapsed_ms" in stage
    llm_end = [data for e, data in events if e == "llm_end"][-1]
    assert llm_end["duration_ms"] == 1300 and llm_end["kind"] == "tool_call"


def test_chat_stream_error_event(app_client, login, monkeypatch):
    import app.api.chat_routes as chat_routes

    async def boom(user, history, message):
        raise RuntimeError("模型超时")

    monkeypatch.setattr(chat_routes, "run_chat", boom)
    monkeypatch.setattr(chat_routes, "run_chat_stream", boom)
    admin = login("admin", "hhw123")
    status, events = _read_sse(app_client, {"message": "hi"}, admin)
    assert status == 200
    errors = [data for e, data in events if e == "error"]
    assert errors and "模型超时" in errors[0]["message"]


def test_chat_stream_requires_login(app_client):
    resp = app_client.post("/api/chat/stream", json={"message": "hi"})
    assert resp.status_code == 401


def test_tool_contract_emits_start_and_end(seeded):
    """工具装饰器通过 runtime 事件总线推送 tool_start / tool_end。"""
    from app.memory.runtime import event_sink_context
    from app.tools import platform_tools

    captured: list[tuple[str, dict]] = []

    class Sink:
        def emit(self, event, payload):
            captured.append((event, payload))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sku_id": "ST-92-C1-M", "available": 5})

    client = platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler),
    )
    platform_tools.set_platform_client(client)
    try:
        from app.auth import AuthUser

        user = AuthUser(username="admin", role="admin", department=None)
        tools = {t.name: t for t in platform_tools.build_platform_tools(user)}
        with event_sink_context(Sink()):
            asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-92-C1-M"}))
    finally:
        platform_tools.set_platform_client(None)

    kinds = [k for k, _ in captured]
    assert kinds == ["tool_start", "tool_end"]
    end = captured[1][1]
    assert end["tool"] == "get_inventory"
    assert end["result_id"].startswith("res_")
    assert end["status"] == "ok"
    assert "elapsed_ms" in end


def test_static_ui_has_stream_and_latency_hooks():
    from pathlib import Path

    from app.static_assets import app_features

    root = Path(__file__).resolve().parent.parent
    js = (root / "web/app.js").read_text(encoding="utf-8")
    html = (root / "web/index.html").read_text(encoding="utf-8")
    css = (root / "web/style.css").read_text(encoding="utf-8")
    assert "createProgressCard" in js and "/api/chat/stream" in js
    assert "progress-time" in js
    # 自检按钮已从 UI 移除，但能力代码保留
    assert "openDiagnostics" in js
    assert 'id="btn-diag"' not in html
    assert "m-lat-p50" in html and "m-lat-p95" in html
    assert "card-hit" in html
    assert "appendWarnNote" in js
    # 模型调用事件与实体表常驻区
    assert "llm_start" in js and "llm_end" in js
    assert "working-set-underbar-body" in js and 'id="working-set-underbar"' in html
    assert "badge-mem" in html and "badge-mem" in css
    assert "run_timeline" in app_features() and "llm_events" in app_features()
    assert "working_set_underbar" in app_features()
    # 页面与脚本版本号必须是同一份（本次升到 ui9：顶栏存储徽标带上向量库）
    assert 'data-ui-build="v20261110-ui9"' in html
    assert 'UI_BUILD = "v20261110-ui9"' in js
    # 弹窗内容过长时在窗口内滚动，不超出视口
    assert "max-height: calc(100vh" in css
    assert "overflow-y: auto" in css
