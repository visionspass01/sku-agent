"""Agent 注册表测试：manifest 校验、版本、工具子集、热加载、坏文件容错。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture()
def registry_dir(tmp_path, _settings, monkeypatch):
    """隔离的注册表目录：只放测试用的 manifest，避免读到仓库里的真实 agent。"""
    from app.agents import registry as reg_mod

    prompts = Path(__file__).resolve().parent.parent / "src" / "app" / "agents" / "prompts"
    monkeypatch.setattr(_settings, "agents_registry_dir", str(tmp_path))
    monkeypatch.setattr(_settings, "agents_hot_reload", True)
    reg_mod.reset_registry()
    yield tmp_path, prompts
    reg_mod.reset_registry()


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


BASE = {
    "id": "analyst",
    "name": "分析助手",
    "prompt": "chat_system",
    "tools": ["*"],
    "default": True,
}


def test_loads_manifest_and_computes_version(registry_dir):
    from app.agents.registry import get_registry

    root, _ = registry_dir
    _write(root / "analyst.json", BASE)
    registry = get_registry()
    manifest = registry.get("analyst")
    assert manifest is not None
    assert manifest.tool_names == ["*"]
    assert len(manifest.version) == 8 and len(manifest.prompt_version) == 8
    assert registry.default_id() == "analyst"


def test_tool_groups_resolve_to_tool_names(registry_dir):
    from app.agents.registry import get_registry

    root, _ = registry_dir
    _write(root / "analyst.json", BASE)
    _write(root / "diagnostician.json", {
        "id": "diagnostician", "name": "诊断", "prompt": "diagnostician",
        "tools": ["read", "skill"],
    })
    registry = get_registry()
    tools = registry.get("diagnostician").tool_names
    assert "get_inventory" in tools and "load_skill" in tools
    assert "create_replenishment_order" not in tools  # 只读 agent 拿不到写工具


def test_missing_prompt_is_reported_not_fatal(registry_dir):
    """坏 manifest 只登记错误，不影响其它 Agent 启动。"""
    from app.agents.registry import get_registry

    root, _ = registry_dir
    _write(root / "analyst.json", BASE)
    _write(root / "broken.json", {"id": "broken", "prompt": "not_exist_prompt"})
    registry = get_registry()
    assert registry.get("analyst") is not None
    assert registry.get("broken") is None
    errors = registry.errors()
    assert errors and "提示词文件不存在" in errors[0]["error"]


def test_unknown_tool_is_rejected(registry_dir):
    from app.agents.registry import get_registry

    root, _ = registry_dir
    _write(root / "analyst.json", BASE)
    _write(root / "bad_tool.json", {"id": "bad_tool", "prompt": "chat_system",
                                    "tools": ["no_such_tool"]})
    registry = get_registry()
    assert registry.get("bad_tool") is None
    assert any("未知工具" in e["error"] for e in registry.errors())


def test_duplicate_id_keeps_first_and_reports(registry_dir):
    from app.agents.registry import get_registry

    root, _ = registry_dir
    _write(root / "a_analyst.json", BASE)
    _write(root / "b_dupe.json", BASE)  # 同 id，字典序靠后，应被拒绝
    registry = get_registry()
    assert registry.get("analyst").version  # 仍然可用
    assert any("id 重复" in e["error"] for e in registry.errors())


def test_version_changes_with_content(registry_dir, monkeypatch):
    from app.agents import registry as reg_mod

    root, prompts = registry_dir
    _write(root / "analyst.json", BASE)
    first = reg_mod.get_registry().get("analyst").version
    # 改提示词内容 + 触发 mtime 变化 → 版本号应变化
    # 注意：必须按字节读写。`Path.write_text` 在 Windows 会把 `\n` 翻成 `\r\n`，
    # 一跑测试就把仓库里的提示词改成 CRLF（P2-3：prompt_version 会因此跳变）。
    path = prompts / "chat_system.md"
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\n<!-- touched -->\n")
        mtime = time.time() + 1
        import os

        os.utime(path, (mtime, mtime))
        reg_mod.get_registry().reload()
        assert reg_mod.get_registry().get("analyst").version != first
    finally:
        path.write_bytes(original)


def test_hot_reload_picks_up_new_manifest(registry_dir):
    """丢一个 json 就多一个 agent——这就是"热插拔"的核心断言。"""
    from app.agents.registry import get_registry

    root, _ = registry_dir
    _write(root / "analyst.json", BASE)
    registry = get_registry()
    assert [m.id for m in registry.all()] == ["analyst"]

    _write(root / "diagnostician.json", {
        "id": "diagnostician", "name": "诊断", "prompt": "diagnostician",
        "tools": ["read"],
    })
    time.sleep(0.01)
    # 目录 mtime 变化 → 下一次 maybe_reload 自动生效（不需要重启、不需要显式 reload）
    changed = registry.maybe_reload()
    assert changed is True
    assert [m.id for m in registry.all()] == ["analyst", "diagnostician"]


def test_hot_reload_detects_edit_of_existing_manifest(registry_dir):
    """回归：修改**已存在**的 manifest 也要触发重载。

    只盯父目录 mtime 是不够的——改文件内容不会改变目录 mtime（线上验收时抓到过），
    因此指纹里必须包含 registry 内文件的最大 mtime。
    """
    import os

    from app.agents.registry import get_registry

    root, _ = registry_dir
    _write(root / "analyst.json", BASE)
    registry = get_registry()
    assert registry.get("analyst").max_tokens == 2500

    payload = dict(BASE, max_tokens=800)
    _write(root / "analyst.json", payload)
    future = time.time() + 30
    os.utime(root / "analyst.json", (future, future))

    assert registry.maybe_reload() is True
    assert registry.get("analyst").max_tokens == 800


def test_disable_agent_via_manifest(registry_dir):
    from app.agents.registry import get_registry

    root, _ = registry_dir
    _write(root / "analyst.json", BASE)
    _write(root / "diagnostician.json", {
        "id": "diagnostician", "name": "诊断", "prompt": "diagnostician",
        "tools": ["read"], "enabled": False,
    })
    registry = get_registry()
    assert [m.id for m in registry.all()] == ["analyst"]
    assert "diagnostician" in registry.health()["agents"]  # health 里仍可见（便于排查）


def test_real_registry_has_three_agents(_settings, monkeypatch):
    """仓库自带的注册表：analyst / diagnostician / operator 且无加载错误。

    `diagnostician` 声明了 memory 组（含写工具 save_analysis）但没声明 write 组，
    加载期会**剔除写工具并记一条 warning**（P2-4）——这里只要求没有 error，
    并把这个已知 warning 钉住（说明"只读 Agent 拿到了写工具"这件事不会再静默发生）。
    """
    from app.agents import registry as reg_mod

    monkeypatch.setattr(_settings, "agents_registry_dir", "")
    reg_mod.reset_registry()
    registry = reg_mod.get_registry()
    ids = [m.id for m in registry.all()]
    assert set(ids) == {"analyst", "diagnostician", "operator"}
    assert registry.default_id() == "analyst"
    assert [e for e in registry.errors() if e["level"] == "error"] == []
    diag = registry.get("diagnostician")
    assert diag is not None and diag.stripped_write_tools == ["save_analysis"]
    warnings = [e["error"] for e in registry.errors() if e["level"] == "warning"]
    assert any("save_analysis" in w for w in warnings), warnings
    reg_mod.reset_registry()
