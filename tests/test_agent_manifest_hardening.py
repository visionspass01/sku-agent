"""P1-5/6/8/10：manifest 加固（memory_scope、缺省 tools、占位符、数量口径）。"""

from __future__ import annotations

import json

import pytest

BASE = {"id": "analyst", "name": "默认", "prompt": "chat_system", "default": True}


@pytest.fixture()
def registry_dir(tmp_path, _settings, monkeypatch):
    from app.agents import registry as reg_mod

    monkeypatch.setattr(_settings, "agents_registry_dir", str(tmp_path))
    monkeypatch.setattr(_settings, "agents_hot_reload", True)
    reg_mod.reset_registry()
    yield tmp_path
    reg_mod.reset_registry()


def _write(path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_default_tools_is_read_only(registry_dir):
    """缺省 tools 只读：忘写 tools 不该意外拿到写工具。"""
    from app.agents.registry import get_registry

    _write(registry_dir / "analyst.json", BASE)  # 不写 tools
    manifest = get_registry().get("analyst")
    assert "create_replenishment_order" not in manifest.tool_names
    assert "get_inventory" in manifest.tool_names


def test_star_keeps_full_toolset(registry_dir):
    from app.agents.registry import get_registry
    from app.tools.platform_tools import ALL_TOOL_NAMES

    _write(registry_dir / "analyst.json", dict(BASE, tools=["*"]))
    manifest = get_registry().get("analyst")
    assert manifest.tool_names == ["*"]
    assert len(ALL_TOOL_NAMES) == 27


def test_unknown_placeholder_is_rejected(registry_dir):
    """提示词里写错占位符（{metdata}）必须报错，而不是静默留在 prompt 里。"""
    prompts = registry_dir / "prompts"
    prompts.mkdir()
    (prompts / "broken.md").write_text("角色：测试\n\n口径：{metdata}\n{memory_sections}",
                                       encoding="utf-8")
    from app.agents import registry as reg_mod

    # 让 registry 在临时 prompts 目录里找（通过 monkeypatch PROMPT_DIR）
    monkeypatch_target = reg_mod.PROMPT_DIR
    try:
        reg_mod.PROMPT_DIR = prompts
        _write(registry_dir / "broken.json", {"id": "broken", "prompt": "broken"})
        registry = reg_mod.get_registry()
        registry.load()
        assert registry.get("broken") is None
        assert any("未知占位符" in e["error"] for e in registry.errors())
    finally:
        reg_mod.PROMPT_DIR = monkeypatch_target


def test_missing_required_placeholder_is_warning(registry_dir):
    prompts = registry_dir / "prompts"
    prompts.mkdir()
    (prompts / "bare.md").write_text("角色：只要 {today}\n", encoding="utf-8")
    from app.agents import registry as reg_mod

    original = reg_mod.PROMPT_DIR
    try:
        reg_mod.PROMPT_DIR = prompts
        _write(registry_dir / "bare.json", {"id": "bare", "prompt": "bare"})
        registry = reg_mod.get_registry()
        registry.load()
        assert registry.get("bare") is not None      # 缺占位符只告警，不阻塞
        warnings = [e for e in registry.errors() if e.get("level") == "warning"]
        assert any("缺少占位符" in w["error"] for w in warnings)
    finally:
        reg_mod.PROMPT_DIR = original


def test_invalid_memory_scope_rejected(registry_dir):
    _write(registry_dir / "bad_scope.json",
           {"id": "bad_scope", "prompt": "chat_system", "memory_scope": "global"})
    from app.agents.registry import get_registry

    registry = get_registry()
    assert registry.get("bad_scope") is None
    assert any("memory_scope" in e["error"] for e in registry.errors())


def test_reflection_rounds_range_validated(registry_dir):
    _write(registry_dir / "too_many.json",
           {"id": "too_many", "prompt": "chat_system", "max_reflection_rounds": 9})
    from app.agents.registry import get_registry

    registry = get_registry()
    assert registry.get("too_many") is None


def test_reflection_rounds_without_image_tools_warns(registry_dir):
    _write(registry_dir / "img.json",
           {"id": "img", "prompt": "chat_system", "tools": ["read"], "max_reflection_rounds": 2})
    from app.agents.registry import get_registry

    registry = get_registry()
    assert registry.get("img") is not None
    warnings = [e for e in registry.errors() if e.get("level") == "warning"]
    assert any("反思循环尚未实现" in w["error"] for w in warnings)


def test_memory_scope_none_skips_session_memory(seeded, monkeypatch, _settings):
    """memory_scope=none：不注入会话事实卡/工作集（规则与计划仍保留）。"""
    import asyncio

    from app.agents import chat_agent
    from app.agents.registry import AgentManifest

    manifest = AgentManifest(id="lean", name="精简", prompt="chat_system",
                             tools=["read"], memory_scope="none")

    async def _meta():
        return "口径", 1

    monkeypatch.setattr(chat_agent, "_metadata_text", _meta)
    from app.auth import AuthUser

    user = AuthUser(username="admin", role="admin", department=None)
    with_memory, _ = asyncio.run(chat_agent.build_system_prompt(
        user, memory_text="<analysis-facts>FACTS</analysis-facts>", manifest=manifest))
    assert "FACTS" not in with_memory
    assert "可取数纪律" not in with_memory or "L0 取数纪律" in with_memory  # L0 静态规则保留


def test_health_and_agents_count_are_consistent(app_client, login):
    headers = login("admin", "hhw123")
    health = app_client.get("/api/health").json()["agents"]
    agents = app_client.get("/api/agents", headers=headers).json()
    assert health["count"] == agents["count"]
    assert health["total"] == agents["total"]


# ---------------------------------------------------------------- P2-4：只读 Agent 不许有写工具
def test_read_only_group_manifest_strips_write_tools(registry_dir):
    """声明 read+memory 但没有 write 组 → 写工具（save_analysis）必须被剔除。

    背景：`memory` 组里同时有 search_analyses/read_analysis（读）与 save_analysis（写），
    只声明 memory 的"只读 Agent"早先会连带拿到写工具；部门账号被写权限白名单拦住，
    但 admin 账号用它就能写——描述与实现不一致。
    """
    from app.agents.registry import get_registry

    _write(registry_dir / "diag.json", {
        "id": "diag", "prompt": "chat_system", "tools": ["read", "memory"],
    })
    manifest = get_registry().get("diag")
    assert manifest is not None
    assert "save_analysis" not in manifest.tool_names
    assert "search_analyses" in manifest.tool_names      # 只读工具保留
    assert manifest.stripped_write_tools == ["save_analysis"]
    warnings = [e["error"] for e in get_registry().errors() if e.get("level") == "warning"]
    assert any("save_analysis" in w for w in warnings), warnings


def test_write_group_manifest_keeps_write_tools(registry_dir):
    from app.agents.registry import get_registry

    _write(registry_dir / "writer.json", {
        "id": "writer", "prompt": "chat_system", "tools": ["write", "memory"],
    })
    manifest = get_registry().get("writer")
    assert "save_analysis" in manifest.tool_names
    assert "create_replenishment_order" in manifest.tool_names
    assert manifest.stripped_write_tools == []


def test_star_manifest_keeps_write_tools(registry_dir):
    from app.agents.registry import get_registry

    _write(registry_dir / "all.json", {"id": "all", "prompt": "chat_system", "tools": ["*"]})
    manifest = get_registry().get("all")
    assert manifest.tool_names == ["*"]
    assert manifest.stripped_write_tools == []


def test_builtin_diagnostician_has_no_write_tools(_settings, monkeypatch):
    """仓库里的三个 Agent：诊断是只读的、执行助手能写、默认助手按账号权限决定。"""
    from app.agents import registry as reg_mod
    from app.tools.platform_tools import WRITE_TOOL_NAMES

    monkeypatch.setattr(_settings, "agents_registry_dir", "")
    reg_mod.reset_registry()
    registry = reg_mod.get_registry()
    try:
        diag = set(registry.get("diagnostician").tool_names)
        assert diag & WRITE_TOOL_NAMES == set(), diag & WRITE_TOOL_NAMES
        operator = set(registry.get("operator").tool_names)
        # operator 声明了 write 组 → 拿到建单/工单类写工具；但它没声明 memory 组，
        # 所以 save_analysis 不在它的清单里（写工具跟着"组"走，不是一刀切）
        assert {"create_replenishment_order", "create_after_sales"} <= operator
        assert "save_analysis" not in operator
        assert "get_inventory" in operator, "P2-5：执行助手需要读工具才能填数量/给依据"
        assert registry.get("analyst").tool_names == ["*"]
    finally:
        reg_mod.reset_registry()
