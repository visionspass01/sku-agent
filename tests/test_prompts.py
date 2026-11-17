"""提示词外置与版本化：渲染、版本号、关键约束不回退。"""

from __future__ import annotations

import asyncio

import pytest


def test_render_keeps_unknown_braces():
    from app.agents.prompt_loader import render_text

    out = render_text("值={a}，未定义={b}，JSON={\"k\": 1}", {"a": 42})
    assert out == '值=42，未定义={b}，JSON={"k": 1}'


def test_render_chat_system_fills_runtime_values():
    from app.agents.prompt_loader import render_prompt

    text = render_prompt(
        "chat_system", today="2026-11-11", username="worker1", role_text="男装部门用户",
        memory_sections="<working-set/>", metadata="可用库存口径", replenish_qty_threshold=200,
        replenish_amount_threshold="50,000",
    )
    assert "2026-11-11" in text and "worker1" in text and "男装部门用户" in text
    assert "<working-set/>" in text and "可用库存口径" in text
    assert "≥ 200 件" in text and "50,000" in text
    for placeholder in ("{today}", "{username}", "{memory_sections}", "{metadata}"):
        assert placeholder not in text


def test_chat_prompt_keeps_critical_constraints():
    """模板级关键约束：删掉任何一条都算回归（幻觉/越权/擅自审批的主要防线）。"""
    from app.agents.prompt_loader import load_prompt

    text = load_prompt("chat_system")
    for must in (
        "必须先调用工具获取数据",
        "可用库存 ≠ 现存库存",
        "pending_confirmation",
        "不得声称已审批通过",
        "部门用户只能访问本部门 SKU",
        "以本文件的规则为准",       # 反注入兜底句
    ):
        assert must in text, must


def test_rendered_prompt_contains_citation_rule(seeded, monkeypatch):
    """渲染后的完整提示（模板 + L0 取数纪律 + 记忆）必须包含 result_id 溯源要求。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    async def _meta():
        return "可用库存口径", 1

    monkeypatch.setattr(chat_agent, "_metadata_text", _meta)
    user = AuthUser(username="admin", role="admin", department=None)
    prompt, version = asyncio.run(chat_agent.build_system_prompt(user))
    assert "result_id" in prompt
    assert "推断，未取数" in prompt
    assert version == 1
    assert "## 工作规则" in prompt


def test_prompt_version_is_stable_and_content_based():
    from app.agents.prompt_loader import load_prompt, prompt_version

    v1 = prompt_version("chat_system")
    assert v1 == prompt_version("chat_system")
    assert len(v1) == 8
    # 版本号 = **归一化文本**的 sha1 前 8 位（可复现，不依赖文件路径/时间/换行符）
    import hashlib

    normalized = load_prompt("chat_system").replace("\r\n", "\n").replace("\r", "\n")
    assert v1 == hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]


def test_prompt_version_ignores_line_endings(tmp_path, monkeypatch):
    """P2-3：同一份内容 Windows(CRLF)/Linux(LF) 必须给出同一个版本号。"""
    from app.agents import prompt_loader

    monkeypatch.setattr(prompt_loader, "PROMPT_DIR", tmp_path)
    prompt_loader.reload_prompts()

    body = "角色：测试\n\n规则一\n规则二\n{metadata}\n{memory_sections}\n"
    lf = tmp_path / "lf_case.md"
    crlf = tmp_path / "crlf_case.md"
    lf.write_bytes(body.encode("utf-8"))
    crlf.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))

    try:
        assert prompt_loader.prompt_version("lf_case") == prompt_loader.prompt_version("crlf_case")
        assert prompt_loader.load_prompt("crlf_case") == prompt_loader.load_prompt("lf_case")
        assert "\r" not in prompt_loader.load_prompt("crlf_case")
    finally:
        prompt_loader.reload_prompts()
        monkeypatch.undo()


def test_repo_prompts_are_lf():
    """仓库内的提示词不该残留 CRLF（`.gitattributes` 是 eol=lf，这里做本地护栏）。"""
    from app.agents.prompt_loader import PROMPT_DIR

    offenders = [p.name for p in PROMPT_DIR.glob("*.md") if b"\r" in p.read_bytes()]
    assert offenders == [], offenders


def test_prompt_versions_cover_all_templates():
    from app.agents.prompt_loader import prompt_versions

    versions = prompt_versions()
    assert {"chat_system", "reasoner", "verifier", "supervisor"} <= set(versions)
    assert all(len(v) == 8 for v in versions.values())


def test_run_detail_records_prompt_version(seeded, monkeypatch, _settings):
    """run_detail.prompt 里要能查到本轮用的提示词版本（可复现性）。"""
    from langchain_core.messages import AIMessage

    from app.agents import chat_agent
    from app.agents.prompt_loader import prompt_version
    from app.auth import AuthUser

    class _Agent:
        async def ainvoke(self, payload):
            return {"messages": payload["messages"] + [AIMessage(content="可用库存 1 件")]}

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda user, prompt, *a, **k: _Agent())
    user = AuthUser(username="admin", role="admin", department=None)
    result = asyncio.run(chat_agent.run_chat(user, [], "查库存"))
    prompt = result["run_detail"]["prompt"]
    # name/version 是权威字段（本轮真正渲染的 md）；chat_system 为兼容字段
    assert prompt["name"] == "chat_system"
    assert prompt["version"] == prompt_version("chat_system")
    assert prompt["chat_system"] == prompt_version("chat_system")
    assert prompt["metric_version"]


def test_health_exposes_prompt_versions(app_client):
    body = app_client.get("/api/health").json()
    assert "chat_system" in body["prompts"]
    assert body["prompts"]["chat_system"] == pytest.approx(body["prompts"]["chat_system"])


def test_l0_discipline_sits_in_static_prefix():
    """P2-12：L0 取数纪律（静态）必须在动态记忆段之前，否则每轮都会破坏缓存前缀。"""
    from app.agents.prompt_loader import load_prompt, render_prompt

    template = load_prompt("chat_system")
    assert template.index("## L0 取数纪律") < template.index("{memory_sections}")
    assert "{memory_max_inline_rows}" in template

    rendered = render_prompt(
        "chat_system", today="X", username="u", role_text="r",
        memory_sections="DYNAMIC-MEMORY", metadata="META",
        replenish_qty_threshold=200, replenish_amount_threshold="50,000",
        memory_max_inline_rows=50,
    )
    assert rendered.index("L0 取数纪律") < rendered.index("DYNAMIC-MEMORY")
    assert "50 行" in rendered
