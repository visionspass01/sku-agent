"""后台合并（档案主文档）的可观测性与 SLO，以及 per-agent 看板（评审 2.2-8 / 2.2-9 / 2.4-14）。

两块都属于"后台在做的事没人看得见"这一类：
- 档案合并是 fire-and-forget，失败只写 jsonl；现在必须有积压条数、最老年龄、失败率与降级判定；
- 多 Agent 一起在线时，"谁慢、谁贵、谁老降级"必须能从 `/api/metrics/agents` 直接读出来。
"""

from __future__ import annotations

import datetime as dt

import pytest


@pytest.fixture(autouse=True)
def _reset_stats():
    from app.memory import merge_stats

    merge_stats.reset()
    from app.pipeline.metrics import reset_agent_cache

    reset_agent_cache()
    yield
    merge_stats.reset()
    reset_agent_cache()


# ---------------------------------------------------------------- merge 指标
def test_merge_stats_counts_outcomes():
    from app.memory import merge_stats

    merge_stats.record("ok", elapsed_ms=120)
    merge_stats.record("ok", elapsed_ms=80)
    merge_stats.record("failed", elapsed_ms=500, error="boom")
    merge_stats.record("abandoned", error="cas_exhausted")
    merge_stats.record("stale")
    snapshot = merge_stats.snapshot([])
    assert snapshot["attempts"] == 5
    assert snapshot["ok"] == 2
    assert snapshot["failed"] == 1
    assert snapshot["abandoned"] == 1
    assert snapshot["stale"] == 1
    assert snapshot["success_rate"] == pytest.approx(0.4)
    assert snapshot["p95_ms"] > 0
    assert snapshot["degraded"] is False
    assert "slo" in snapshot


def test_merge_degraded_when_backlog_is_old():
    from app.memory import merge_stats

    old = (dt.datetime.now() - dt.timedelta(minutes=30)).isoformat(timespec="seconds")
    snapshot = merge_stats.snapshot([{"enqueued_at": old}, {"enqueued_at": old}])
    assert snapshot["pending"] == 2
    assert snapshot["oldest_pending_minutes"] >= 29
    assert snapshot["degraded"] is True, "积压超过 SLO 阈值必须显式降级"


def test_drain_records_stats_and_reports_remaining():
    """真实的 drain 会把每条结果登记进指标，并把 remaining 一起返回。"""
    from app.memory import org_memory

    result = __import__("asyncio").run(org_memory.adrain_pending_merges(limit=1))
    assert {"processed", "ok", "failed", "remaining", "merge_stats"} <= set(result)
    assert result["merge_stats"]["attempts"] >= 0


def test_health_exposes_merge_slo(app_client):
    body = app_client.get("/api/health").json()
    assert "merge" in body
    merge = body["merge"]
    assert {"pending", "degraded", "slo", "success_rate"} <= set(merge)
    assert merge["degraded"] is False


def test_metrics_exposes_merge_and_components(app_client, login):
    headers = login("admin", "hhw123")
    body = app_client.get("/api/metrics", headers=headers).json()
    for key in ("memory", "mcp", "platform", "rag", "agents", "trace"):
        assert key in body, f"/api/metrics 缺少 {key} 块"
    assert "merge" in body["memory"]
    assert "capabilities" in body["platform"]
    assert "vector_store" in body["rag"]


# ---------------------------------------------------------------- per-agent 看板
def _seed_turn(session, *, agent_id: str, elapsed_ms: int, tokens=(10, 20),
               cost=0.01, grounding="passed", fallback=False, username="admin") -> None:
    from app.agents import chat_store

    row = chat_store.create_session(session, username)
    chat_store.save_turn_log(session, row.id, 0, username, {
        "turn_id": f"t_{agent_id}_{elapsed_ms}",
        "elapsed_ms": elapsed_ms,
        "plan": {"plan_id": "p1", "intent": "lookup", "confidence": 0.9},
        "llm_usage": {"input_tokens": tokens[0], "output_tokens": tokens[1], "cost_cny": cost},
        "grounding": {"status": grounding},
        "agent": {"id": agent_id, "version": "v1", "model_used": "deepseek-flash",
                  "fallback_used": fallback},
        "steps": [{"tool": "get_inventory"}, {"tool": "get_sales"}],
    })


def test_agent_turn_stats_aggregates(seeded):
    """按增量断言：测试库是共享的（其它文件也会往 analyst 写轮次），绝对值不可靠。"""
    from app.agents.chat_store import agent_turn_stats
    from app.db.engine import session_scope

    with session_scope() as session:
        before = agent_turn_stats(session, hours=24)
        base = before["agents"].get("analyst", {"turns": 0, "tool_calls": 0, "tokens_in": 0})
        base_diag = before["agents"].get("diagnostician", {"turns": 0, "grounding": {}})
        _seed_turn(session, agent_id="analyst", elapsed_ms=1000)
        _seed_turn(session, agent_id="analyst", elapsed_ms=3000)
        _seed_turn(session, agent_id="diagnostician", elapsed_ms=500, fallback=True,
                   grounding="degraded")
        after = agent_turn_stats(session, hours=24)

    analyst = after["agents"]["analyst"]
    assert analyst["turns"] - base["turns"] == 2
    assert analyst["tool_calls"] - base["tool_calls"] == 4
    assert analyst["tokens_in"] - base["tokens_in"] == 20
    assert analyst["p95_ms"] >= 1000
    assert analyst["avg_tools_per_turn"] > 0
    # 新增的两轮都是 passed（共享库里其它用例可能有别的状态，所以按增量断言）
    base_passed = base.get("grounding", {}).get("passed", 0)
    assert analyst["grounding"].get("passed", 0) - base_passed == 2

    diag = after["agents"]["diagnostician"]
    assert diag["turns"] - base_diag["turns"] == 1
    assert diag["fallback_rate"] > 0
    assert (diag["grounding"].get("degraded", 0)
            - base_diag["grounding"].get("degraded", 0)) == 1


def test_agent_metrics_endpoint_scopes_by_department(seeded, app_client, login):
    from app.agents.chat_store import agent_turn_stats
    from app.db.engine import session_scope

    with session_scope() as session:
        _seed_turn(session, agent_id="mens-agent", elapsed_ms=800, username="worker1")
        _seed_turn(session, agent_id="women-agent", elapsed_ms=900, username="worker2")

    admin = app_client.get("/api/metrics/agents", headers=login("admin", "hhw123")).json()
    assert {"mens-agent", "women-agent"} <= set(admin["agents"])
    assert admin["scope"]["role"] == "admin"

    mens = app_client.get("/api/metrics/agents", headers=login("worker1", "hhw111")).json()
    assert "mens-agent" in mens["agents"]
    assert "women-agent" not in mens["agents"]
    assert mens["scope"]["department"] == "男装"

    with session_scope() as session:
        direct = agent_turn_stats(session, hours=24, departments=["男装"])
    assert "women-agent" not in direct["agents"]


def test_agent_metrics_requires_login(app_client):
    assert app_client.get("/api/metrics/agents").status_code == 401


def test_metrics_agent_block_is_cached(app_client, login):
    """/api/metrics 每 3s 被轮询：per-agent 聚合必须走 30s 缓存，不能每次全表扫。"""
    from app.pipeline import metrics as metrics_mod

    headers = login("admin", "hhw123")
    first = app_client.get("/api/metrics", headers=headers).json()["agents"]
    metrics_mod._AGENT_CACHE["at"] = 0.0     # 模拟缓存过期后重算
    second = app_client.get("/api/metrics", headers=headers).json()["agents"]
    assert set(first["agents"]) == set(second["agents"])
    assert metrics_mod._AGENT_CACHE["at"] > 0
