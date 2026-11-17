"""Agent 路由测试：golden set 准确率、显式覆盖、解释类兜底、禁用回退、API 端点。"""

from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATASET = ROOT / "data" / "eval" / "routing.jsonl"


def _registry():
    from app.agents.registry import get_registry

    registry = get_registry()
    registry.reload()
    return registry


def _route_case(registry, case: dict) -> dict:
    """题集里 `plan_intent` 的用例按生产路径带上 query_plan 一起路由（复审 P5）。"""
    plan = {"intent": case["plan_intent"]} if case.get("plan_intent") else None
    return registry.route(case["message"], plan)


def test_golden_set_accuracy_at_least_90_percent():
    """路由是确定性规则，可以做离线回归：改关键词/加 agent 前后都跑这套题。"""
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(cases) >= 15
    assert any(case.get("plan_intent") for case in cases), "题集要覆盖 intent 分支"
    registry = _registry()
    hits = 0
    missed: list[str] = []
    for case in cases:
        actual = _route_case(registry, case)["agent_id"]
        if actual == case["expect"]:
            hits += 1
        else:
            missed.append(f"{case['id']}: 期望 {case['expect']} 实际 {actual}（{case['message']}）")
    accuracy = hits / len(cases)
    assert accuracy >= 0.9, f"路由准确率 {accuracy:.2%} 低于 90%：{missed}"


def test_golden_set_declares_tool_capabilities():
    """P2-5：题集要能表达「派对了还不够，还得有工具」——这类缺陷路由指标抓不到。"""
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(case.get("expect_tools") for case in cases), "题集缺少 expect_tools 断言"
    assert any(case.get("forbid_tools") for case in cases), "题集缺少 forbid_tools 断言"
    registry = _registry()
    for case in cases:
        manifest = registry.get(_route_case(registry, case)["agent_id"])
        assert manifest is not None, case["id"]
        tools = set(manifest.tool_names)
        wildcard = tools == {"*"}
        for name in case.get("expect_tools") or []:
            assert wildcard or name in tools, f"{case['id']}：{manifest.id} 缺少 {name}"
        for name in case.get("forbid_tools") or []:
            assert not wildcard and name not in tools, f"{case['id']}：{manifest.id} 不该有 {name}"


def test_operator_can_read_before_writing():
    """operator 建单前要能查库存/测算建议（P2-5：模板里只给写工具等于"只能拍脑袋"）。"""
    registry = _registry()
    tools = set(registry.get("operator").tool_names)
    assert {"get_inventory", "get_sku_metrics", "get_sell_through"} <= tools
    assert "create_replenishment_order" in tools


def test_routing_by_keyword_symptom():
    registry = _registry()
    assert registry.route("这个款断码了，要补多少")["agent_id"] == "diagnostician"
    assert registry.route("帮我建一张补货单")["agent_id"] == "operator"
    assert registry.route("这单物流到哪了")["agent_id"] == "operator"


def test_explain_question_goes_to_default_agent():
    """概念解释类问题不该被"安全库存"这类指标词带进诊断 Agent。"""
    registry = _registry()
    decision = registry.route("帮我解释一下安全库存的计算口径")
    assert decision["agent_id"] == registry.default_id()
    assert decision["routed_by"] == "explain:default"


def test_routing_by_plan_intent():
    """复审 P5：manifest 的 `intents` 必须是 query_plan 真会产出的值，意图分支才不是死代码。"""
    registry = _registry()
    decision = registry.route("帮我处理一下", {"intent": "approval"})
    assert decision["agent_id"] == "operator"
    assert decision["routed_by"].startswith("intent:")
    diagnose = registry.route("帮我处理一下", {"intent": "diagnose"})
    assert diagnose["agent_id"] == "diagnostician"
    # 关键词优先于意图：有明确症状词时不看 intent
    assert registry.route("这个款断码了", {"intent": "approval"})["agent_id"] == "diagnostician"


def test_manifest_intents_must_be_known(tmp_path, _settings, monkeypatch):
    """拼错的 intent 在**加载期**报错（而不是静默变成死分支）。"""
    from app.agents import registry as reg_mod

    (tmp_path / "analyst.json").write_text(json.dumps({
        "id": "analyst", "name": "默认", "prompt": "chat_system", "tools": ["*"], "default": True,
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "broken.json").write_text(json.dumps({
        "id": "broken", "prompt": "chat_system", "tools": ["read"],
        "routing": {"keywords": ["断码"], "intents": ["diagnosis"]},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(_settings, "agents_registry_dir", str(tmp_path))

    reg = reg_mod.AgentRegistry()
    reg.load()

    errors = [e for e in reg.errors() if e.get("level") == "error"]
    assert any("非法意图" in e["error"] for e in errors), errors
    assert reg.get("broken") is None           # 坏文件不进注册表
    assert reg.get("analyst") is not None      # 好文件照常加载


def test_default_for_unmatched_question():
    registry = _registry()
    decision = registry.route("你好，你能做什么")
    assert decision["agent_id"] == registry.default_id()
    assert decision["routed_by"] == "default"


def test_explicit_agent_id_wins():
    from app.agents.chat_agent import resolve_agent

    info = resolve_agent("operator", "这个款断码了吗")
    assert info["agent_id"] == "operator"
    assert info["routed_by"] == "explicit"


def test_unknown_agent_falls_back_to_default():
    from app.agents.chat_agent import resolve_agent

    info = resolve_agent("no_such_agent", "你好")
    assert info["agent_id"] == "analyst"
    assert info["fallback_from"] == "no_such_agent"


def test_agents_enabled_false_forces_default_agent(_settings, monkeypatch):
    """`AGENTS_ENABLED=false` 必须真的只剩默认 Agent（显式 id / 关键词路由都不生效）。"""
    from app.agents.chat_agent import resolve_agent

    monkeypatch.setattr(_settings, "agents_enabled", False)
    # 显式指定 operator：仍回落到默认 Agent，并记录 fallback_from
    info = resolve_agent("operator", "这个款断码了吗")
    assert info["agent_id"] == "analyst"
    assert info["routed_by"] == "agents_disabled"
    assert info["fallback_from"] == "operator"
    # 关键词本该路由到 diagnostician，但总开关关掉后也只走默认
    info2 = resolve_agent(None, "这个款断码了吗")
    assert info2["agent_id"] == "analyst"
    assert info2["routed_by"] == "agents_disabled"


def test_platform_mcp_token_is_read_from_settings(_settings, monkeypatch):
    """`PLATFORM_MCP_TOKEN` 之前是"写了不生效"的变量：Settings 补字段后 MCP 才能用它。"""
    from app.mcp_servers import _platform

    monkeypatch.setattr(_settings, "platform_internal_token", "main-token")
    monkeypatch.setattr(_settings, "platform_mcp_token", "")
    assert _platform._token() == "main-token", "留空时应回落主令牌"
    monkeypatch.setattr(_settings, "platform_mcp_token", "mcp-token")
    assert _platform._token() == "mcp-token", "配了就应该用 MCP 专用令牌"


def test_disabled_agent_falls_back(tmp_path, _settings, monkeypatch):
    """被禁用的 Agent 不能被路由到（回退到默认并记录 fallback_from）。"""
    from app.agents import chat_agent
    from app.agents import registry as reg_mod

    prompts = ROOT / "src" / "app" / "agents" / "prompts"
    monkeypatch.setattr(_settings, "agents_registry_dir", str(tmp_path))
    (tmp_path / "analyst.json").write_text(json.dumps({
        "id": "analyst", "name": "默认", "prompt": "chat_system", "tools": ["*"], "default": True,
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "diagnostician.json").write_text(json.dumps({
        "id": "diagnostician", "name": "诊断", "prompt": "diagnostician",
        "tools": ["read"], "enabled": False,
        "routing": {"keywords": ["断码"]},
    }, ensure_ascii=False), encoding="utf-8")
    reg_mod.reset_registry()
    try:
        info = chat_agent.resolve_agent("diagnostician", "这个款断码了吗")
        assert info["agent_id"] == "analyst"
        assert info["fallback_from"]
    finally:
        reg_mod.reset_registry()
    assert prompts.exists()


def test_agents_endpoint_lists_three(app_client, login):
    headers = login("worker1", "hhw111")
    data = app_client.get("/api/agents", headers=headers).json()
    ids = {item["id"] for item in data["items"]}
    assert {"analyst", "diagnostician", "operator"} <= ids
    assert data["default"] == "analyst"
    # 加载期只允许 warning（例如 P2-4「只读 Agent 已剔除写工具」），不能有 error
    assert [e for e in data["errors"] if e.get("level") == "error"] == []
    assert app_client.get("/api/agents").status_code == 401


def test_reload_endpoint_admin_only(app_client, login):
    worker = login("worker1", "hhw111")
    admin = login("admin", "hhw123")
    assert app_client.post("/api/admin/agents/reload", headers=worker).status_code == 403
    resp = app_client.post("/api/admin/agents/reload", headers=admin)
    assert resp.status_code == 200
    body = resp.json()
    assert body["reloaded"] is True
    assert body["registry"]["count"] >= 3


def test_health_exposes_agents(app_client):
    body = app_client.get("/api/health").json()
    assert body["agents"]["count"] >= 3
    assert body["agents"]["default"] == "analyst"
    assert "versions" not in body["agents"]  # 关键字段：count/default/agents/errors
    assert isinstance(body["agents"]["agents"], dict)
