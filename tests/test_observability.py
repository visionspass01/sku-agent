"""定时任务默认关闭 + 运行详情（run_detail）可观测性测试。"""

from __future__ import annotations

import asyncio
import datetime as dt
import os

import httpx
import pytest


# ---------------------------------------------------------------- 定时任务默认关闭
def test_scheduler_default_off(_settings):
    from app import scheduler as sched
    from app.config import Settings

    assert Settings().scheduler_enabled is False
    # 未启用时不应创建 job，也不启动调度器
    assert sched.start_scheduler() is None
    status = sched.scheduler_status()
    assert status["enabled"] is False
    assert status["jobs"] == []
    assert "关闭" in status["note"]


class _FakeJob:
    def __init__(self, func, job_id: str) -> None:
        self.func = func
        self.id = job_id
        self.trigger = "fake-cron"
        self.next_run_time = dt.datetime(2026, 11, 10, 9, 0, 0)


class _FakeScheduler:
    def __init__(self, timezone=None) -> None:
        self.timezone = timezone
        self.jobs: list[_FakeJob] = []
        self.running = False

    def add_job(self, func, trigger, id=None, replace_existing=True):
        self.jobs.append(_FakeJob(func, id or "job"))

    def start(self) -> None:
        self.running = True

    def get_jobs(self) -> list[_FakeJob]:
        return self.jobs

    def shutdown(self, wait: bool = False) -> None:
        self.running = False


def test_scheduler_status_when_enabled(_settings, monkeypatch):
    from app import scheduler as sched

    monkeypatch.setattr(_settings, "scheduler_enabled", True)
    monkeypatch.setattr(sched, "AsyncIOScheduler", _FakeScheduler)
    try:
        sched.start_scheduler()
        status = sched.scheduler_status()
        assert status["enabled"] is True
        assert status["running"] is True
        assert len(status["jobs"]) == 2
        assert all(job["next_run"] for job in status["jobs"])
        assert status["limits"] == {"hourly": 5, "daily": 50}
    finally:
        sched.stop_scheduler()
    assert sched.scheduler_status()["jobs"] == []


def test_health_exposes_scheduler_state(app_client):
    data = app_client.get("/api/health").json()
    assert data["scheduler_enabled"] is False
    assert data["scheduler_next_run"] is None
    assert "关闭" in data["scheduler_note"]
    status = app_client.get("/api/scheduler/status").json()
    assert status["enabled"] is False and status["running"] is False


def test_scheduler_run_once_permissions(app_client, login):
    assert app_client.post("/api/scheduler/run-once?job=daily").status_code == 401
    worker = login("worker1", "hhw111")
    assert app_client.post("/api/scheduler/run-once?job=daily", headers=worker).status_code == 403


# ---------------------------------------------------------------- 运行详情
@pytest.fixture()
def offline_platform(monkeypatch):
    """中台不可用：让元数据接口走 FALLBACK，测试完全离线。"""
    from app.tools import platform_tools

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "offline"})

    client = platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler),
    )
    platform_tools.set_platform_client(client)
    yield client
    platform_tools.set_platform_client(None)


class _FakeAgent:
    async def ainvoke(self, payload):
        from langchain_core.messages import AIMessage

        return {"messages": payload["messages"] + [AIMessage(content="可用库存 42 件（res_abc123def4）")]}


def test_run_detail_has_plan_memory_and_permission(seeded, offline_platform, monkeypatch):
    """run_detail 覆盖计划 / 记忆快照 / token 用量 / 权限，且与响应字段互不干扰。"""
    from app.agents import chat_agent
    from app.auth import AuthUser
    from app.memory.runtime import chat_turn_context

    monkeypatch.setattr(chat_agent, "build_chat_agent", lambda user, prompt, *a, **k: _FakeAgent())
    user = AuthUser(username="worker1", role="user", department="男装")
    with chat_turn_context(session_id="s-test-turn", include_query_plan=True):
        result = asyncio.run(chat_agent.run_chat(
            user, [{"role": "user", "content": "ST-92-C1-M 库存"}], "ST-92-C1-M 当前可用库存？",
        ))
    detail = result["run_detail"]
    assert detail["turn_id"].startswith("t_")
    assert detail["plan"]["plan_id"].startswith("qp_")
    assert detail["plan"]["intent"] == "lookup"
    assert detail["llm"]["model"]
    assert "context" in detail["memory"] and "previous" in detail["memory"]
    assert detail["permission"]["department"] == "男装"
    assert detail["permission"]["allowed"] == ["男装"]
    assert result["query_plan"] is not None          # 勾选时回传
    assert result["cited_result_ids"] == ["res_abc123def4"]


def test_turn_usage_accumulates_without_touching_global_stats(_settings):
    from app.llm import add_turn_usage, get_llm, turn_usage_context, turn_usage_snapshot

    llm = get_llm()
    before = dict(llm.stats)
    with turn_usage_context():
        add_turn_usage(120, 30)
        add_turn_usage(80, 20)
        snap = turn_usage_snapshot()
    assert snap == {"calls": 2, "input_tokens": 200, "output_tokens": 50, "errors": 0}
    assert llm.stats == before  # 全局口径不受影响
    assert turn_usage_snapshot()["calls"] == 0  # 上下文退出后不残留


def test_turn_log_persisted_and_replayed(app_client, login, monkeypatch):
    """一轮对话后 chat_turn_log 落库，历史接口回放当轮 run_detail。"""
    import app.api.chat_routes as chat_routes

    async def fake_run_chat(user, history, message):
        return {
            "reply": "可用库存 42 件（res_deadbeef01）",
            "tool_trace": [],
            "metadata_version": 1,
            "query_plan": None,
            "clarify": None,
            "cited_result_ids": ["res_deadbeef01"],
            "memory": {"facts_version": 1, "working_version": 1},
            "session_references": [],
            "run_detail": {
                "turn_id": "t_fake000001",
                "elapsed_ms": 1200,
                "plan": {"plan_id": "qp_x", "intent": "lookup", "confidence": 0.8},
                "steps": [{"seq": 1, "tool": "get_inventory", "status": "ok",
                           "elapsed_ms": 300, "result_id": "res_deadbeef01",
                           "as_of": "2026-11-04", "row_count": 0, "truncated": False}],
                "llm": {"model": "deepseek-flash", "calls": 2, "input_tokens": 100,
                        "output_tokens": 20, "errors": 0},
                "memory": {"context": {"facts_card": "<analysis-facts>v1</analysis-facts>",
                                       "working_set": "<working-set>SKU: ST-13-C1-M</working-set>",
                                       "facts_version": 1, "working_version": 1, "trigger": "rules"},
                           "previous": {"facts_version": 0, "working_version": 0},
                           "facts_delta": 1},
                "references": {"result_ids": ["res_deadbeef01"], "artifacts": [], "analyses": []},
                "permission": {"department": "管理员", "allowed": None, "denied_steps": []},
            },
        }

    monkeypatch.setattr(chat_routes, "run_chat", fake_run_chat)
    admin = login("admin", "hhw123")
    resp = app_client.post("/api/chat", headers=admin, json={"message": "ST-13-C1-M 库存"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_detail"]["turn_id"] == "t_fake000001"
    assert body["run_detail"]["steps"][0]["result_id"] == "res_deadbeef01"

    sid = body["session_id"]
    hist = app_client.get(f"/api/chat/{sid}", headers=admin).json()
    assistant = [m for m in hist["messages"] if m["role"] == "assistant"]
    assert assistant and assistant[-1]["run_detail"]["memory"]["context"]["facts_version"] == 1
    assert "analysis-facts" in assistant[-1]["run_detail"]["memory"]["context"]["facts_card"]

    snap = app_client.get(f"/api/memory/sessions/{sid}", headers=admin).json()
    assert snap["turn_count"] >= 1


def test_turn_log_pruned_to_recent(seeded):
    from app.agents import chat_store
    from app.db.engine import session_scope

    with session_scope() as session:
        sid = chat_store.create_session(session, "admin").id
        for i in range(chat_store.TURN_LOG_KEEP + 5):
            msg = chat_store.append_message(session, sid, "assistant", f"第{i}轮")
            chat_store.save_turn_log(session, sid, msg.id, "admin",
                                     {"turn_id": f"t{i}", "plan": {}, "memory": {}, "steps": []})
        assert chat_store.count_turn_logs(session, sid) == chat_store.TURN_LOG_KEEP
        logs = chat_store.get_turn_logs(session, sid)
        assert len(logs) == chat_store.TURN_LOG_KEEP


def test_static_ui_contains_run_detail_hooks():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    html = (root / "web/index.html").read_text(encoding="utf-8")
    js = (root / "web/app.js").read_text(encoding="utf-8")
    assert "chk-run-detail" in html and "btn-memory" in html and "chat-history" in html
    # 批量巡检：定时任务说明文字已从 UI 移除（代码保留，状态仍可从 /api/health 查）
    assert "scheduler-badge" not in html
    assert "renderSchedulerBadge" in js
    assert "attachRunDetail" in js and "showRunDetail" in js
    assert "visibilitychange" in js
    assert "/api/chat/sessions" in js and "/api/memory/sessions/" in js
    # 版本自检：HTML 与脚本的 build tag 必须一致，且页面内置旧脚本兜底提示
    html_build = html.split('data-ui-build="')[1].split('"')[0]
    js_build = js.split('const UI_BUILD = "')[1].split('"')[0]
    assert html_build == js_build
    assert "stale-reload-inline" in html
    # 交互入口用事件委托 + bindEvents 容错，避免一个元素缺失导致整体失效
    assert 'closest("#btn-memory")' in js
    assert 'e.target.id === "chat-history"' in js
    assert "bindGlobalHandlers" in js
    assert "bindEvents 初始化失败" in js
    # 新会话不清空旧会话（旧会话可在历史里回放）；无会话时记忆面板给出选择器
    assert "已开启新会话。之前聊过的会话保留" in js
    assert "openMemorySessionPicker" in js
    assert 'focusin' in js
    # 两个开关的产物要能区分：蓝标 Query 重写 / 绿标 显示运行详情 / 黄标 当前事实卡
    assert "artifact-badge" in js and "badge-plan" in js and "badge-run" in js
    assert "artifact-foot-plan" in js and "artifact-foot-run" in js
    assert "facts-underbar" in html and "facts-underbar-body" in html
    assert "badge-facts" in html
    assert "updateFactsUnderbarFromDetail" in js and "refreshFactsUnderbar" in js


def test_static_assets_are_not_cached(app_client):
    """静态资源必须回源校验：避免"HTML 新、app.js 旧"导致新功能静默失效。"""
    for path in ("/", "/app.js", "/style.css", "/index.html", "/agents.html", "/agents.js"):
        resp = app_client.get(path)
        assert resp.status_code == 200, path
        assert "no-cache" in resp.headers.get("cache-control", ""), path


def test_ui_smoke_script_passes(_settings):
    """用最小 DOM 垫片真跑一遍前端交互（无浏览器）：node scripts/ui_smoke.js。"""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("未安装 node，跳过 UI 冒烟")
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [node, str(root / "scripts" / "ui_smoke.js")],
        capture_output=True, text=True, timeout=120, cwd=str(root),
        encoding="utf-8", errors="replace", env=env,
    )
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    assert "全部通过" in (proc.stdout or "")
