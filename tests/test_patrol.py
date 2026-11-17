"""批量巡检日志与简报测试。"""

from __future__ import annotations

import asyncio

from fakes import FakeLLM


def _patch_llm(fake: FakeLLM) -> None:
    import app.llm as llm_mod

    llm_mod._llm = fake


def test_patrol_log_and_report(seeded):
    from sqlalchemy import select

    from app.db.engine import session_scope
    from app.db.models import PatrolLog
    from app.pipeline.patrol import run_patrol

    _patch_llm(FakeLLM(passed=True))
    summary = asyncio.run(run_patrol(mode="daily", limit=1, operator="alice"))

    assert "report" in summary and summary["report"]["summary"]
    assert summary["report"]["operator"] == "alice"
    assert summary["report"]["counts"]["processed"] == 1

    with session_scope() as session:
        log = session.scalar(select(PatrolLog).where(PatrolLog.run_id == summary["run_id"]))
        assert log is not None
        assert log.operator == "alice"
        assert log.mode == "daily"
        assert log.processed == 1
        assert log.report["summary"]
        assert log.report["next_steps"]
        assert log.created_at is not None
        # 巡检日志应同时可在 repos 侧按时间倒序查询
        assert log.id


def test_patrol_log_api(app_client, login):
    """通过 API 查看最近的巡检日志/简报（需要登录）。"""
    headers = login("admin", "hhw123")
    resp = app_client.get("/api/patrol/recent?limit=10", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    if data["items"]:
        item = data["items"][0]
        assert set(["run_id", "operator", "mode", "report"]).issubset(item.keys())

def test_patrol_progress_registry(seeded):
    """巡检结束后进度注册表应有最终阶段与处理进度。"""
    import uuid

    from app.pipeline import progress as patrol_progress
    from app.pipeline.patrol import run_patrol

    _patch_llm(FakeLLM(passed=True))
    run_id = str(uuid.uuid4())
    summary = asyncio.run(run_patrol(mode="daily", limit=1, operator="alice", run_id=run_id))
    p = patrol_progress.get(run_id)
    assert p is not None
    assert p["stage"] == "finished"
    assert p["processed"] == summary["processed"]
    assert p["total_selected"] == summary["selected"]
    assert p["params"]["operator"] == "alice"
    patrol_progress.clear(run_id)


def test_patrol_termination(seeded):
    """终止信号触发后：输出已巡检/未巡检与中断原因，并落库报告。"""
    import uuid

    from sqlalchemy import select

    from app.db.engine import session_scope
    from app.db.models import PatrolLog
    from app.pipeline import progress as patrol_progress
    from app.pipeline.patrol import run_patrol

    _patch_llm(FakeLLM(passed=True))
    run_id = str(uuid.uuid4())
    patrol_progress.get_stop_event(run_id).set()  # 提前置位：应立即中断

    summary = asyncio.run(run_patrol(mode="daily", limit=3, operator="bob", run_id=run_id))
    assert summary["interrupted"] is True
    assert summary["run_status"] == "cancelled"
    assert summary["interrupt_reason"] == "用户中断"
    assert summary["inspected_count"] == 0
    assert summary["not_inspected_count"] == summary["selected"]
    assert "终止" in summary["report"]["summary"]

    with session_scope() as session:
        log = session.scalar(select(PatrolLog).where(PatrolLog.run_id == run_id))
        assert log is not None
        assert log.report["interrupted"] is True
        assert log.report["not_inspected_count"] == summary["selected"]

    prog = patrol_progress.get(run_id)
    assert prog["stage"] == "cancelled"
    assert prog["interrupted"] is True
    patrol_progress.clear(run_id)
