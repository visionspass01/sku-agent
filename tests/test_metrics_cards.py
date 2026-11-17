"""三张指标卡的数据口径：LLM（对话+巡检+来源）、队列（巡检 + 对话进行中）、缓存（RAG 接口）。"""

from __future__ import annotations

import asyncio


# ---------------------------------------------------------------- LLM 统计口径
def test_record_usage_counts_global_department_and_source(_settings):
    from app.llm import get_llm, record_usage

    llm = get_llm()
    record_usage(calls=2, input_tokens=1000, output_tokens=200,
                 department="管理员", source="chat")
    record_usage(calls=1, input_tokens=300, output_tokens=50,
                 department="男装", source="patrol")
    assert llm.stats["calls"] == 3
    assert llm.stats["input_tokens"] == 1300
    assert llm.department_stats["管理员"]["calls"] == 2
    assert llm.department_stats["男装"]["calls"] == 1
    assert llm.source_stats["chat"]["calls"] == 2
    assert llm.source_stats["chat"]["input_tokens"] == 1000
    assert llm.source_stats["patrol"]["calls"] == 1


def test_metrics_llm_exposes_sources(app_client, _settings):
    from app.llm import record_usage

    record_usage(calls=2, input_tokens=800, output_tokens=120, department="管理员", source="chat")
    data = app_client.get("/api/metrics").json()
    assert data["llm"]["calls"] >= 2
    assert data["llm"]["sources"]["chat"]["calls"] == 2
    assert data["llm"]["departments"]["管理员"]["calls"] >= 2


def test_chat_turn_records_llm_usage(seeded, monkeypatch):
    """对话 Agent 用自己的 ChatOpenAI：run_chat 结束后要把用量补登记到全局统计。"""
    from langchain_core.messages import AIMessage

    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.llm import get_llm
    from app.memory.runtime import chat_turn_context

    class FakeAgent:
        async def ainvoke(self, payload):
            msg = AIMessage(content="可用库存 693 件（res_abc123def4）")
            msg.usage_metadata = {"input_tokens": 1234, "output_tokens": 234}
            return {"messages": payload["messages"] + [msg]}

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda user, prompt, *a, **k: FakeAgent())
    before = dict(get_llm().stats)
    user = AuthUser(username="admin", role="admin", department=None)
    with chat_turn_context(session_id="s-llm-usage", include_query_plan=False):
        asyncio.run(chat_agent.run_chat(user, [{"role": "user", "content": "hi"}], "ST-01-C1-M 库存"))
    after = get_llm().stats
    assert after["calls"] == before["calls"] + 1
    assert after["input_tokens"] == before["input_tokens"] + 1234
    assert after["output_tokens"] == before["output_tokens"] + 234
    assert get_llm().source_stats["chat"]["calls"] >= 1


# ---------------------------------------------------------------- 队列卡片口径
def test_queue_card_reports_chat_streams(app_client):
    from app.observability import runtime_state

    data = app_client.get("/api/metrics").json()
    assert data["queue"]["chat_streams"] == runtime_state.active_chat_streams() == 0
    assert "对话" in data["queue"]["note"]

    runtime_state.chat_stream_started()
    try:
        shown = app_client.get("/api/metrics").json()["queue"]["chat_streams"]
        assert shown == 1
    finally:
        runtime_state.chat_stream_finished()
    assert app_client.get("/api/metrics").json()["queue"]["chat_streams"] == 0


def test_sse_gauge_returns_to_zero(app_client, login, monkeypatch):
    """流式对话执行期间计数为 1，流结束后（生成器 finally）归零。"""
    import app.api.chat_routes as chat_routes
    from app.observability import runtime_state

    seen: dict[str, int] = {}

    async def fake_run_chat(user, history, message):
        seen["during"] = runtime_state.active_chat_streams()
        return {"reply": "ok", "tool_trace": [], "metadata_version": 1, "query_plan": None,
                "clarify": None, "cited_result_ids": [], "memory": {},
                "session_references": [], "run_detail": None}

    monkeypatch.setattr(chat_routes, "run_chat", fake_run_chat)
    monkeypatch.setattr(chat_routes, "run_chat_stream", fake_run_chat)
    admin = login("admin", "hhw123")
    with app_client.stream("POST", "/api/chat/stream", json={"message": "hi"}, headers=admin) as resp:
        events = list(resp.iter_text())
    assert events, "流式响应为空"
    assert seen.get("during") == 1, seen
    assert runtime_state.active_chat_streams() == 0


def test_metrics_queue_keeps_patrol_fields(app_client):
    data = app_client.get("/api/metrics").json()
    for key in ("pending", "length", "departments", "chat_streams", "note"):
        assert key in data["queue"], key


# ---------------------------------------------------------------- 缓存：RAG 检索接口
def test_rag_search_uses_cache(app_client, seeded):
    from app.cache.ttl_cache import get_ttl_cache

    first = app_client.get("/api/rag/search", params={"q": "大促期间断码如何处理", "topk": 3})
    assert first.status_code == 200
    second = app_client.get("/api/rag/search", params={"q": "大促期间断码如何处理", "topk": 3})
    assert second.status_code == 200
    assert first.json()["count"] == second.json()["count"]
    stats = get_ttl_cache().stats.snapshot()
    assert stats["hits"] >= 1 and stats["misses"] >= 1


# ---------------------------------------------------------------- 前端布局/卡片静态校验
def test_layout_uses_balanced_columns():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    html = (root / "web/index.html").read_text(encoding="utf-8")
    css = (root / "web/style.css").read_text(encoding="utf-8")
    js = (root / "web/app.js").read_text(encoding="utf-8")
    # 布局：AI 助手独占整行 + 其余面板多列均衡
    assert 'class="chat-row"' in html and 'class="panels"' in html
    assert 'class="layout"' not in html
    assert "columns: 2 460px" in css and "break-inside: avoid" in css
    # 行尾不再有 \r\r\n
    assert html.encode("utf-8").count(b"\r\r\n") == 0
    # 卡片口径说明与对话进行中计数
    assert 'class="muted cards-note"' in html
    assert 'id="m-chat-streams"' in html
    assert "renderQueueCard" in js and "renderCacheCard" in js
    assert "来源分布" in js and "对话通道（SSE 进行中）" in js
