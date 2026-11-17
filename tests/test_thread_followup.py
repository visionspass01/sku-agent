"""分析 thread 多轮（LangGraph checkpointer）：追问复用上一轮数据，不重复取数。

验收口径：
- 第二轮（followup）**MCP 调用次数不增加**（没有重新采集）；
- trace 里出现 `refine` 节点；
- 追问指令真的进了 reasoner 的 prompt；
- 没跑过研判的 SKU 追问 → 404（ThreadNotFoundError）。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fakes import FakeLLM

from app.db import repos
from app.db.engine import session_scope
from app.pipeline.patrol import ThreadNotFoundError, followup_sku, inspect_sku


def _patch_llm(fake: FakeLLM) -> None:
    import app.llm as llm_mod

    llm_mod._llm = fake


def _run_first_inspection(sku_id: str = "ST-90-C1-M") -> str:
    async def run():
        with session_scope() as session:
            run = repos.create_run(session, kind="single", params={"sku_id": sku_id})
            run_id = run.id
        result = await inspect_sku(sku_id, run_id)
        return run_id, result

    run_id, result = asyncio.run(run())
    assert result["status"] == "ok", result.get("error")
    assert result["thread_id"] == f"sku:{sku_id}"
    return run_id


def test_followup_reuses_thread_without_mcp_calls(seeded, monkeypatch):
    fake = FakeLLM(passed=True)
    _patch_llm(fake)

    from app.mcp_client import client as mcp_client

    counter = {"n": 0}
    original_call = mcp_client.MCPHub.call

    async def counting_call(self, server, tool, arguments=None):
        counter["n"] += 1
        return await original_call(self, server, tool, arguments)

    monkeypatch.setattr(mcp_client.MCPHub, "call", counting_call)

    run_id = _run_first_inspection("ST-90-C1-M")
    calls_after_first = counter["n"]
    assert calls_after_first > 0, "第一轮应真实调用 MCP"

    # thread_id 已落库，前端可据此追问
    with session_scope() as session:
        run = repos.get_run(session, run_id)
        assert (run.params or {}).get("thread_id") == "sku:ST-90-C1-M"

    result = asyncio.run(followup_sku("ST-90-C1-M", "那优先调拨还是补货？"))
    assert result["thread_id"] == "sku:ST-90-C1-M"
    # 核心断言：追问没有触发新的 MCP 采集
    assert counter["n"] == calls_after_first, "追问不应重新调用 MCP 取数"

    trace_nodes = [t.get("node") for t in (result["final_state"].get("trace") or [])]
    assert "refine" in trace_nodes
    assert result["final_state"].get("plan"), "追问应产出新的预案"
    # 追问指令进入 reasoner prompt（reasoner 之后 verifier 也会调 LLM，所以看全部 prompt）
    assert any("那优先调拨还是补货？" in p for p in fake.prompts)
    assert any("本轮是多轮追问" in p for p in fake.prompts)


def test_followup_requires_existing_thread(seeded):
    _patch_llm(FakeLLM(passed=True))
    with pytest.raises(ThreadNotFoundError):
        asyncio.run(followup_sku("ST-91-C1-M", "那清仓还是补货？"))


def test_followup_resets_revision_budget_and_stale_issues(seeded):
    """复审 F3：追问是"新一轮问题"——重写预算重置，上一轮的校验 issues 不再注入。

    回归背景：`refine` 早先只写 trace，`attempts`/`verification` 从首轮继承下来，
    于是追问里校验一失败就直接 finalize（0 次重写机会），而且 reasoner 的 prompt 里
    还带着上一轮的 issues（模型会去修上一轮的问题）。
    """
    # 首轮全部通过（attempts 停在 1，verification 是"通过"）
    _patch_llm(FakeLLM(passed=True))
    _run_first_inspection("ST-90-C1-M")

    # 追问：第一次校验失败 → 必须回到 reasoner 重写；第二次通过
    fake = FakeLLM(passed_sequence=[False, True])
    _patch_llm(fake)
    result = asyncio.run(followup_sku("ST-90-C1-M", "那优先调拨还是补货？"))

    assert fake.schema_calls.count("InspectionPlan") == 2, fake.schema_calls
    assert fake.schema_calls.count("VerifierVerdict") == 2, fake.schema_calls
    assert result["final_state"].get("status") == "ok"

    # 追问轮的第一个 reasoner prompt 不该带"上一轮校验未通过"（旧 issues 已清空）
    first_plan_prompt = fake.prompts[0]
    assert "本轮是多轮追问" in first_plan_prompt
    assert "上一轮校验未通过" not in first_plan_prompt
    # 但重写那一轮应该带上（本轮自己的 issues），否则"回流修正"就没意义
    assert any("上一轮校验未通过" in p for p in fake.prompts[1:])

    # attempts 从 0 起算：两次 reasoner 之后是 2，而不是首轮的 1 + 2 = 3
    assert result["final_state"].get("attempts") == 2


@pytest.fixture()
def followup_ctx(seeded, app_client, login, monkeypatch):
    """HTTP 场景：FakeLLM + 管理员登录头（后台任务在 TestClient 内同步跑完）。"""
    fake = FakeLLM(passed=True)
    _patch_llm(fake)
    return app_client, login("admin", "hhw123"), fake


def test_followup_endpoint_end_to_end(followup_ctx):
    app_client, admin, _fake = followup_ctx
    resp = app_client.post("/api/inspect", headers=admin, json={"sku_id": "ST-90-C1-M"})
    assert resp.status_code == 200, resp.text
    run_id = resp.json()["run_id"]
    detail: dict = {}
    for _ in range(100):
        detail = app_client.get(f"/api/inspections/{run_id}", headers=admin).json()
        if detail.get("results"):
            break
        time.sleep(0.05)
    assert detail.get("results"), detail

    follow = app_client.post("/api/inspect/followup", headers=admin, json={
        "sku_id": "ST-90-C1-M", "instruction": "那优先调拨还是补货？",
    })
    assert follow.status_code == 200, follow.text
    body = follow.json()
    assert body["thread_id"] == "sku:ST-90-C1-M"
    assert body["plan"]["risk_level"] in ("低", "中", "高")
    assert "refine" in [t.get("node") for t in body["trace"]]
    assert "复用" in body["note"]


def test_followup_endpoint_404_without_thread(followup_ctx):
    app_client, admin, _fake = followup_ctx
    resp = app_client.post("/api/inspect/followup", headers=admin, json={
        "sku_id": "ST-91-C1-M", "instruction": "那清仓还是补货？",
    })
    assert resp.status_code == 404
    assert "线程" in resp.text
