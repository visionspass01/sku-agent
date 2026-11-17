"""端到端多轮追问（评审 E2）：同一会话连跑三轮，验证记忆/指代/历史在**真链路**上生效。

为什么需要：`tests/test_memory_layers.py` 验的是 `refresh_memory` / `build_memory_context`
这些函数级行为，`scripts/verify_three_turns.py` 只 `print` 不打分。于是"多轮追问"一直没有
断言：指代有没有回填、工作集有没有进系统提示、事实卡版本有没有推进、历史会不会把本轮问题
重复注入——这些都只有真跑三轮才看得出。这里用 `/api/chat` + 脚本模型覆盖，零网络。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

SKU = "ST-01-C1-M"
ANSWER_1 = "可用库存 510 件。"
ANSWER_2 = "它还有 510 件可用。"
ANSWER_3 = "近 7 天日均销量约 20 件。"


class _ScriptedModel(BaseChatModel):
    """按脚本回答，并把每次调用看到的 messages 记下来（供断言系统提示/历史）。"""

    steps: Any = None
    cursor: Any = None
    seen: Any = None

    @property
    def _llm_type(self) -> str:
        return "scripted-turns"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self

    def bind(self, **kwargs):  # noqa: ANN003
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None,  # noqa: ANN001
                  **kwargs) -> ChatResult:
        self.seen.append([(m.type, str(m.content)) for m in messages])
        index = self.cursor[0]
        assert index < len(self.steps), "脚本步数用完：模型调用次数超出预期"
        self.cursor[0] = index + 1
        return ChatResult(generations=[ChatGeneration(message=self.steps[index])])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        return self._generate(messages, stop, run_manager, **kwargs)


def _tool_call() -> AIMessage:
    return AIMessage(content="", tool_calls=[{
        "name": "get_inventory", "args": {"sku_id": SKU}, "id": "c1", "type": "tool_call",
    }])


def _platform():
    from app.tools import platform_tools

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/inventory"):
            return httpx.Response(200, json={
                "sku_id": SKU, "on_hand": 600, "locked": 30, "reserved_crm": 40,
                "order_hold": 20, "available": 510, "warehouse_id": "WH-HZ",
                "as_of": "2026-11-03", "cached": False,
            })
        if request.url.path.endswith("/sales"):
            return httpx.Response(200, json={"sku_id": SKU, "days": 7, "daily_avg_7d": 20.0})
        return httpx.Response(404, json={"detail": "not found"})

    platform_tools.set_platform_client(platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler)))


@pytest.fixture()
def scripted(monkeypatch, _settings):
    """三轮对话的脚本：T1 先调工具再回答，T2/T3 直接回答。"""
    from app.agents import chat_agent

    seen: list[list[tuple[str, str]]] = []
    steps = [_tool_call(), AIMessage(content=ANSWER_1),
             AIMessage(content=ANSWER_2), AIMessage(content=ANSWER_3)]
    cursor = [0]
    monkeypatch.setattr(_settings, "grounding_enabled", False)
    monkeypatch.setattr(chat_agent, "ChatOpenAI",
                        lambda *a, **k: _ScriptedModel(steps=steps, cursor=cursor, seen=seen))
    _platform()
    yield seen
    from app.tools import platform_tools

    platform_tools.set_platform_client(None)


def _say(client, headers, message: str, session_id: str | None = None) -> dict:
    payload: dict = {"message": message, "include_query_plan": True}
    if session_id:
        payload["session_id"] = session_id
    resp = client.post("/api/chat", headers=headers, json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_three_turns_keep_session_memory(app_client, login, scripted):
    headers = login("admin", "hhw123")

    first = _say(app_client, headers, f"{SKU} 当前可用库存")
    session_id = first["session_id"]
    assert [s["tool"] for s in first["run_detail"]["steps"]] == ["get_inventory"]
    assert first["reply"] == ANSWER_1

    second = _say(app_client, headers, "它还有多少", session_id)
    assert second["reply"] == ANSWER_2

    third = _say(app_client, headers, "近 7 天销量怎么样", session_id)
    assert third["reply"] == ANSWER_3

    # --- 指代回填：第二轮的查询计划应把 SKU 从工作集里补齐
    plan = second["query_plan"]
    assert plan["entities"]["skus"] == [SKU], plan["entities"]
    assert plan["resolved_from"] == "working_set.skus"
    assert any("指代回填" in a for a in plan["assumptions"])

    # --- 工作集/记忆真的进了系统提示（不是只在 run_detail 里好看）
    assert len(scripted) >= 4
    turn2_prompts = [content for kinds in scripted[2:3] for kind, content in kinds if kind == "system"]
    assert turn2_prompts and SKU in turn2_prompts[0]
    assert "本轮查询计划" in turn2_prompts[0]

    # --- 历史不重复注入本轮问题，且按 user/assistant 交替
    last_call = scripted[-1]
    kinds = [kind for kind, _ in last_call]
    contents = [content for _, content in last_call]
    assert kinds[0] == "system"
    assert contents.count(f"{SKU} 当前可用库存") == 1
    assert contents.count("近 7 天销量怎么样") == 1
    assert kinds.count("human") == 3          # 三轮用户消息都在
    assert kinds[-1] == "human"               # 本轮问题排在最后

    # --- 事实卡随历史推进（T1 还没有可压缩的结论，T2 起应该有）
    assert first["run_detail"]["memory"]["facts_version"] == 0
    assert second["run_detail"]["memory"]["facts_version"] >= 1

    # --- 轮次台账落库（trajectory 的数据源）
    from app.agents import chat_store
    from app.db.engine import session_scope

    with session_scope() as session:
        rows = chat_store.load_turn_rows(session, hours=24)
    mine = [row for row in rows if row["session_id"] == session_id]
    assert len(mine) == 3, [row["session_id"] for row in rows]


def test_followup_without_session_does_not_inherit(app_client, login, scripted):
    """另一个会话不该看到上一个会话的工作集（指代解析不能跨会话穿越）。"""
    headers = login("admin", "hhw123")
    _say(app_client, headers, f"{SKU} 当前可用库存")

    other = _say(app_client, headers, "它还有多少")
    assert other["query_plan"]["resolved_from"] != "working_set.skus"
    assert other["query_plan"]["entities"]["skus"] == []
