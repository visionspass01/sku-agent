"""出口溯源校验（grounding）测试：硬规则 + 一次重写 + 降级留痕。"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _enable_grounding(_settings, monkeypatch):
    """本文件专门测出口校验：默认打开（conftest 里全局关闭，避免测试打真实模型）。"""
    monkeypatch.setattr(_settings, "grounding_enabled", True)

STEPS = [{"tool": "get_inventory", "result_id": "res_1a2b3c4d5e", "status": "ok"}]
TRACE = [{"type": "result", "tool": "get_inventory",
          "result": '{"available": 42, "daily_avg_7d": 6.0, "sell_through_days": 7.0}'}]


def _check(reply: str, *, steps=STEPS, trace=TRACE):
    from app.agents.grounding import check_reply, collect_result_ids, tool_result_text

    return check_reply(reply, result_ids=collect_result_ids(steps, trace),
                       tool_text=tool_result_text(trace), tool_calls=len(steps))


def check_plain(reply, *, result_ids, tool_text, tool_calls):  # noqa: ANN001
    """薄封装：直接透传底层判定（便于构造「无工具」场景）。"""
    from app.agents.grounding import check_reply as _impl

    return _impl(reply, result_ids=result_ids, tool_text=tool_text, tool_calls=tool_calls)


def test_check_passes_when_numbers_trace_back():
    verdict = _check("可用库存 42 件（res_1a2b3c4d5e），预计 7 天售罄。")
    assert verdict["passed"] is True
    assert verdict["citations"]["unknown"] == []


def test_check_flags_fabricated_result_id():
    verdict = _check("可用库存 42 件（res_ffffffffffff）")
    assert verdict["passed"] is False
    assert "res_ffffffffffff" in " ".join(verdict["issues"])


def test_check_flags_unsourced_numbers():
    verdict = _check("可用库存 42 件，日均销量 6.0 件，预计 7 天售罄。")
    assert verdict["passed"] is False
    assert any("result_id" in i for i in verdict["issues"])


def test_check_accepts_explicit_inference_marker():
    verdict = _check("可用库存约 42 件（推断，未取数）。")
    assert verdict["passed"] is True


def test_check_flags_all_numbers_missing_from_tools():
    verdict = _check("可用库存 999 件（res_1a2b3c4d5e），日均 888 件，售罄 777 天。")
    assert verdict["passed"] is False
    assert verdict["numbers"]["unverified_count"] == 3


def test_check_flags_numbers_without_any_tool_call():
    """红队：用户要求「不用查了直接说」→ 模型给数字但没调工具，必须判失败。"""
    verdict = check_plain("可用库存 9999 件。", result_ids=set(), tool_text="", tool_calls=0)
    assert verdict["passed"] is False
    assert verdict["skipped"] is False
    assert any("未调用任何工具" in i for i in verdict["issues"])


def test_check_skips_only_when_truly_nothing_to_ground():
    verdict = check_plain("你好，我可以帮你查库存。", result_ids=set(), tool_text="", tool_calls=0)
    assert verdict["passed"] is True and verdict["skipped"] is True


def test_ground_repairs_untaught_numbers_without_tools(monkeypatch):
    """无工具但给数字：走同一条重写通道，改成「推断，未取数」后才算通过。"""
    from app.agents import grounding

    class _LLM:
        def __init__(self):
            self.user_prompt = ""

        async def invoke_text(self, system, user, max_tokens=None):
            self.user_prompt = user
            return "可用库存约 9999 件（推断，未取数）。"

    llm = _LLM()
    monkeypatch.setattr(grounding, "get_llm", lambda: llm)
    out = asyncio.run(grounding.ground_reply("可用库存 9999 件。", steps=[], tool_trace=[]))
    assert out["report"]["status"] == "repaired"
    assert "本轮没有工具结果" in llm.user_prompt
    assert "推断" in out["reply"]


def test_ground_skips_when_no_tools(monkeypatch):
    from app.agents import grounding

    def _boom(*_a, **_k):  # 不应被调用：没有工具结果时不做重写
        raise AssertionError("本轮无工具调用时不应触发 LLM 重写")

    monkeypatch.setattr(grounding, "get_llm", _boom)
    out = asyncio.run(grounding.ground_reply("你好，我可以帮你查库存。", steps=[], tool_trace=[]))
    assert out["report"]["status"] == "skipped"
    assert out["reply"] == "你好，我可以帮你查库存。"


def test_ground_repaired_by_single_rewrite(monkeypatch):
    from app.agents import grounding

    class _LLM:
        def __init__(self):
            self.calls = 0

        async def invoke_text(self, system, user, max_tokens=None):
            self.calls += 1
            assert "本轮工具结果" in user
            return "可用库存 42 件（res_1a2b3c4d5e）。"

    llm = _LLM()
    monkeypatch.setattr(grounding, "get_llm", lambda: llm)
    out = asyncio.run(grounding.ground_reply("可用库存 42 件。", steps=STEPS, tool_trace=TRACE))
    assert llm.calls == 1
    assert out["report"]["status"] == "repaired"
    assert "res_1a2b3c4d5e" in out["reply"]


def test_ground_degrades_when_rewrite_still_bad(monkeypatch):
    from app.agents import grounding

    class _LLM:
        async def invoke_text(self, system, user, max_tokens=None):
            return "可用库存 12345 件。"  # 仍然没有来源、数字也对不上

    monkeypatch.setattr(grounding, "get_llm", lambda: _LLM())
    out = asyncio.run(grounding.ground_reply("可用库存 42 件。", steps=STEPS, tool_trace=TRACE))
    assert out["report"]["status"] == "degraded"
    assert "自检未通过" in out["reply"]


def test_ground_degrades_when_llm_unavailable(monkeypatch):
    from app.agents import grounding
    from app.llm import LLMError

    class _LLM:
        async def invoke_text(self, system, user, max_tokens=None):
            raise LLMError("未配置 DEEPSEEK_API_KEY")

    monkeypatch.setattr(grounding, "get_llm", lambda: _LLM())
    out = asyncio.run(grounding.ground_reply("可用库存 42 件。", steps=STEPS, tool_trace=TRACE))
    assert out["report"]["status"] == "degraded"
    assert "自检未通过" in out["reply"]


def test_ground_respects_disable_switch(monkeypatch, _settings):
    from app.agents import grounding

    monkeypatch.setattr(_settings, "grounding_enabled", False)
    out = asyncio.run(grounding.ground_reply("可用库存 42 件。", steps=STEPS, tool_trace=TRACE))
    assert out["report"]["status"] == "disabled"
    assert out["reply"] == "可用库存 42 件。"


def test_run_detail_exposes_grounding(seeded, monkeypatch):
    """run_chat 要把校验结果带进 run_detail（前端据 status 显示告警条）。"""
    from langchain_core.messages import AIMessage

    from app.agents import chat_agent

    class _FakeAgent:
        async def ainvoke(self, payload):
            return {"messages": payload["messages"] + [AIMessage(content="可用库存 693 件（res_abc123def4）")]}

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda user, prompt, *a, **k: _FakeAgent())
    monkeypatch.setattr(chat_agent, "_metadata_text",
                        lambda: _async_value(("口径", 1)))

    from app.auth import AuthUser

    user = AuthUser(username="admin", role="admin", department=None)
    result = asyncio.run(chat_agent.run_chat(user, [{"role": "user", "content": "hi"}], "查库存"))
    grounding = result["run_detail"]["grounding"]
    assert "status" in grounding
    assert result["reply"]


def _async_value(value):
    async def _inner():
        return value

    return _inner()


@pytest.mark.parametrize("reply", ["", "1. 结论：先看库存", "| SKU | 可用 |\n| --- | --- |"])
def test_check_ignores_non_claims(reply):
    verdict = _check(reply)
    assert verdict["passed"] is True
