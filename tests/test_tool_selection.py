"""工具集声明语义（评审 1.4 / 2.3-12）。

被修的 bug 链条（四步全部静默）：
    manifest tools=["image"] → resolve 出空集合 → tool_names=[] → allow=[] →
    装配时 `not selection` 判成"没声明" → 拿到**全部 22 个工具**，
    而 run_detail 里 tool_count 写的是 0。

这里的每个用例都对应链条上的一环，任何一环回退都会红。
"""

from __future__ import annotations

import pytest


def test_none_and_star_mean_all_tools():
    from app.tools import platform_tools as pt

    assert pt.resolve_tool_selection(None) is None
    assert pt.resolve_tool_selection(["*"]) is None
    assert pt.expected_tool_count(None) == len(pt.ALL_TOOL_NAMES)
    assert pt.expected_tool_count(["*"]) == len(pt.ALL_TOOL_NAMES)


def test_group_declaration_resolves_to_group_members():
    from app.tools import platform_tools as pt

    selected = pt.resolve_tool_selection(["read"])
    assert selected == set(pt.TOOL_GROUPS["read"])
    assert pt.expected_tool_count(["read"]) == len(pt.TOOL_GROUPS["read"])


def test_mixed_group_and_tool_names():
    from app.tools import platform_tools as pt

    selected = pt.resolve_tool_selection(["meta", "get_inventory"])
    assert selected == {"plan_query", "get_inventory"}


def test_empty_declaration_is_rejected():
    """`[]` 曾经等价于"全量"：这是最危险的一条（写操作会一起放行）。"""
    from app.tools import platform_tools as pt

    with pytest.raises(ValueError, match="零工具"):
        pt.resolve_tool_selection([])


def test_reserved_group_is_rejected_loudly():
    """`["image"]` 曾经静默拿到全部 22 个工具——现在必须直接失败。"""
    from app.tools import platform_tools as pt

    with pytest.raises(ValueError, match="预留"):
        pt.resolve_tool_selection(["image"])


def test_typo_is_rejected():
    from app.tools import platform_tools as pt

    with pytest.raises(ValueError, match="未知工具"):
        pt.resolve_tool_selection(["get_inventroy"])


def test_declared_count_equals_assembled_count():
    """run_detail 报的数必须等于真正装配出来的工具数（旧实现分两条路径算，会撒谎）。"""
    from app.tools import platform_tools as pt

    for selection in (None, ["*"], ["read"], ["read", "write"], ["meta"]):
        assembled = pt.build_platform_tools(None, allow=selection)
        assert len(assembled) == pt.expected_tool_count(selection), selection


def test_agent_manifest_with_reserved_group_is_rejected_at_load():
    """走真实的 registry 加载路径：坏 manifest 只记 error，不影响其它 Agent。"""
    import json

    from app.agents.registry import AgentManifest, resolve_tool_selection

    manifest = AgentManifest(id="imagebot", prompt="chat_system", tools=["image"])
    with pytest.raises(ValueError, match="预留"):
        resolve_tool_selection(manifest.tools)
    # 顺带确认 pydantic 层没有偷偷把声明改成别的形状
    assert json.loads(manifest.model_dump_json())["tools"] == ["image"]


def test_registry_isolates_bad_manifest(tmp_path):
    """一个坏 manifest 不能拖垮整个注册表（错误要能在 /api/agents.errors 里看到）。"""
    import json

    from app.agents.prompt_loader import PROMPT_DIR
    from app.agents.registry import AgentRegistry

    (tmp_path / "good.json").write_text(json.dumps({
        "id": "good", "prompt": "chat_system", "tools": ["read"],
    }), encoding="utf-8")
    (tmp_path / "bad.json").write_text(json.dumps({
        "id": "bad", "prompt": "chat_system", "tools": ["image"],
    }), encoding="utf-8")
    registry = AgentRegistry(tmp_path)
    registry.load()
    assert registry.get("good") is not None
    assert registry.get("bad") is None
    errors = registry.errors()
    assert any(item.get("file") == "bad.json" and item.get("level") == "error" for item in errors)
    assert PROMPT_DIR.exists()      # prompt 目录必须存在，否则上面的用例会因别的错失败
