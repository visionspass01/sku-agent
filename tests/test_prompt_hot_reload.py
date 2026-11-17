"""提示词热加载测试：改 md 即生效（不重启），版本号随之变化。"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

PROMPTS = Path(__file__).resolve().parent.parent / "src" / "app" / "agents" / "prompts"


def _write(path: Path, text: str) -> None:
    """按字节写回：`Path.write_text` 在 Windows 会把 `\\n` 翻成 `\\r\\n`，
    等于每跑一次测试就把仓库里的提示词改成 CRLF（P2-3：版本号会因此跳变）。"""
    path.write_bytes(text.encode("utf-8"))


@pytest.fixture()
def touched_prompt():
    """安全地临时改一个提示词文件，测试结束恢复原样。"""
    name = "chat_system.md"
    path = PROMPTS / name
    original = path.read_text(encoding="utf-8").replace("\r\n", "\n")

    def touch(marker: str) -> None:
        _write(path, original + f"\n<!-- {marker} -->\n")
        mtime = time.time() + 60
        os.utime(path, (mtime, mtime))

    yield touch
    _write(path, original)


def test_prompt_version_tracks_content(touched_prompt):
    from app.agents.prompt_loader import prompt_version

    before = prompt_version("chat_system")
    touched_prompt("hot-reload-probe")
    after = prompt_version("chat_system")
    assert after != before


def test_load_prompt_sees_new_content(touched_prompt):
    """reasoner/verifier/supervisor 现在按调用读取，改文件后下一次调用就能读到新内容。"""
    from app.agents.prompt_loader import load_prompt

    before = load_prompt("reasoner")
    path = PROMPTS / "reasoner.md"
    original = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    try:
        _write(path, original + "\n<!-- reasoner-probe -->\n")
        mtime = time.time() + 60
        os.utime(path, (mtime, mtime))
        after = load_prompt("reasoner")
        assert "reasoner-probe" in after and "reasoner-probe" not in before
    finally:
        _write(path, original)


def test_rendered_system_prompt_reflects_edit(touched_prompt, seeded, monkeypatch):
    """端到端：改模板 → 渲染出的系统提示立刻变化（不需要重启进程）。"""
    from app.agents import chat_agent
    from app.auth import AuthUser

    async def _meta():
        return "口径", 1

    monkeypatch.setattr(chat_agent, "_metadata_text", _meta)
    user = AuthUser(username="admin", role="admin", department=None)
    before, _ = asyncio.run(chat_agent.build_system_prompt(user))
    touched_prompt("system-prompt-probe")
    after, _ = asyncio.run(chat_agent.build_system_prompt(user))
    assert "system-prompt-probe" in after and "system-prompt-probe" not in before


def test_registry_versions_refresh_after_prompt_edit(touched_prompt):
    from app.agents.registry import get_registry

    registry = get_registry()
    registry.reload()
    before = registry.get("analyst").prompt_version
    touched_prompt("registry-version-probe")
    registry.reload()
    assert registry.get("analyst").prompt_version != before


def test_reload_prompts_clears_cache(touched_prompt):
    from app.agents import prompt_loader

    prompt_loader.load_prompt("chat_system")
    touched_prompt("cache-clear-probe")
    prompt_loader.reload_prompts()
    assert "cache-clear-probe" in prompt_loader.load_prompt("chat_system")
