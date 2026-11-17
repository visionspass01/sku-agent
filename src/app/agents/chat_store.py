"""多轮对话会话存储（Agent 库）。"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import ChatMemory, ChatMessage, ChatQueryPlan, ChatSession, ChatTurnLog


def create_session(session: Session, username: str, title: str = "新会话") -> ChatSession:
    now = dt.datetime.now()
    row = ChatSession(id=str(uuid.uuid4()), username=username, title=title[:64],
                      created_at=now, updated_at=now)
    session.add(row)
    session.flush()
    return row


def get_session(session: Session, session_id: str, username: str | None = None) -> ChatSession | None:
    row = session.get(ChatSession, session_id)
    if row is None:
        return None
    if username is not None and row.username != username:
        return None
    return row


def list_sessions(session: Session, username: str, limit: int = 20,
                  within_hours: int | None = None) -> list[ChatSession]:
    stmt = select(ChatSession).where(ChatSession.username == username)
    if within_hours:
        cutoff = dt.datetime.now() - dt.timedelta(hours=within_hours)
        stmt = stmt.where(ChatSession.updated_at >= cutoff)
    return list(session.scalars(stmt.order_by(ChatSession.updated_at.desc()).limit(limit)))


def prune_sessions(session: Session, within_hours: int) -> int:
    """清理超过保留时长的会话及其子表数据（消息 / 运行详情 / 查询计划 / 会话记忆）。"""
    cutoff = dt.datetime.now() - dt.timedelta(hours=within_hours)
    stale = list(session.scalars(
        select(ChatSession.id).where(ChatSession.updated_at < cutoff)
    ))
    if not stale:
        return 0
    for table in (ChatMessage, ChatTurnLog, ChatQueryPlan, ChatMemory):
        session.execute(delete(table).where(table.session_id.in_(stale)))
    session.execute(delete(ChatSession).where(ChatSession.id.in_(stale)))
    session.flush()
    return len(stale)


def session_message_counts(session: Session, session_ids: list[str]) -> dict[str, int]:
    """每个会话的消息条数（历史会话下拉只展示真正有内容的会话）。"""
    if not session_ids:
        return {}
    rows = session.execute(
        select(ChatMessage.session_id, func.count())
        .where(ChatMessage.session_id.in_(session_ids))
        .group_by(ChatMessage.session_id)
    ).all()
    return {session_id: int(count) for session_id, count in rows}


def append_message(session: Session, session_id: str, role: str, content: str,
                   tool_trace: list | None = None) -> ChatMessage:
    row = ChatMessage(session_id=session_id, role=role, content=content,
                      tool_trace=tool_trace or [], created_at=dt.datetime.now())
    session.add(row)
    owner = session.get(ChatSession, session_id)
    if owner is not None:
        owner.updated_at = dt.datetime.now()
        if role == "user" and owner.title == "新会话":
            owner.title = content[:30]
    session.flush()
    return row


def get_history(session: Session, session_id: str, limit: int | None = None) -> list[dict]:
    """最近 N 条消息（按时间正序），**保证以用户消息起始**。

    为什么要多取一条再裁（复审 N5）：消息是按条数截断的，若总数是奇数，
    截断后第一条会是 assistant —— 模型看到的对话就从"AI 先开口"开始，
    等于把上一轮问答切了一半。这里把配对看得比条数重要：首条不是 user 就丢掉。
    """
    limit = limit or get_settings().chat_history_limit
    rows = list(session.scalars(
        select(ChatMessage).where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.id.desc()).limit(limit + 1)
    ).all())[::-1]
    if len(rows) > limit:
        rows = rows[-limit:]
    if rows and rows[0].role != "user":
        rows = rows[1:]
    return [{"role": r.role, "content": r.content, "tool_trace": r.tool_trace or []}
            for r in rows]


def list_messages(session: Session, session_id: str) -> list[ChatMessage]:
    return list(session.scalars(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
    ))


def reset_session(session: Session, session_id: str) -> None:
    session.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
    session.execute(delete(ChatTurnLog).where(ChatTurnLog.session_id == session_id))


# ---------------------------------------------------------------- 单轮运行详情
TURN_LOG_KEEP = 50  # 每会话只保留最近 N 轮运行详情，避免事实卡快照无限增长


def save_turn_log(session: Session, session_id: str, message_id: int, username: str,
                  run_detail: dict) -> None:
    plan = run_detail.get("plan") or {}
    agent = run_detail.get("agent") or {}
    usage = run_detail.get("llm_usage") or {}
    grounding = run_detail.get("grounding") or {}
    steps = run_detail.get("steps") or []
    session.add(ChatTurnLog(
        session_id=session_id, message_id=message_id, username=username or "",
        turn_id=run_detail.get("turn_id", ""), plan_id=plan.get("plan_id") or "",
        intent=plan.get("intent") or "", confidence=float(plan.get("confidence") or 0.0),
        elapsed_ms=int(run_detail.get("elapsed_ms") or 0),
        # 聚合维度：宁可重复存一份，也不要让看板去扫 JSON（几万轮后就是全表扫描）
        agent_id=str(agent.get("id") or ""), agent_version=str(agent.get("version") or ""),
        model=str(agent.get("model_used") or agent.get("model") or ""),
        tokens_in=int(usage.get("input_tokens") or 0),
        tokens_out=int(usage.get("output_tokens") or 0),
        cost_cny=float(usage.get("cost_cny") or 0.0),
        tool_calls=len(steps),
        grounding_status=str(grounding.get("status") or ""),
        fallback_used=bool(agent.get("fallback_used")),
        payload=run_detail,
        created_at=dt.datetime.now(),
    ))
    session.flush()
    prune_turn_logs(session, session_id)


def save_aborted_turn(session: Session, *, session_id: str, username: str, turn_id: str,
                      error_code: str, guard: dict | None = None, agent_id: str = "") -> None:
    """记录**被中止**的一轮（工具循环 / 预算 / 步数超限）。

    为什么单独记：中止的轮次没有答复，走不到 `save_turn_log`，于是 trajectory 里的
    `abort_rate` 会永远是 0——「守护到底拦下了多少轮」这件事只能从日志里猜。
    这里复用 `chat_turn_log` 现有列（`grounding_status="aborted"` + payload 带
    `error_code/guard`），**不需要迁移**；记录失败只告警，不能掩盖真正的中止原因。
    """
    guard = guard or {}
    session.add(ChatTurnLog(
        session_id=session_id, message_id=0, turn_id=str(turn_id or ""),
        username=username or "", agent_id=str(agent_id or ""),
        tool_calls=int(guard.get("success_calls") or 0),
        grounding_status="aborted",
        payload={"turn_id": turn_id, "error_code": error_code, "guard": dict(guard)},
        created_at=dt.datetime.now(),
    ))
    session.flush()
    prune_turn_logs(session, session_id)


def load_turn_rows(session: Session, *, hours: int = 24) -> list[dict]:
    """窗口内的轮次行（trajectory 聚合的输入）：只取聚合需要的列，不整行搬 payload。"""
    cutoff = dt.datetime.now() - dt.timedelta(hours=max(1, int(hours)))
    stmt = select(ChatTurnLog).where(ChatTurnLog.created_at >= cutoff)
    return [
        {
            "session_id": row.session_id,
            "turn_id": row.turn_id,
            "username": row.username,
            "agent_id": row.agent_id,
            "grounding_status": row.grounding_status,
            # 中止轮次的 payload 没有 steps，工具数只能从这一列拿（trajectory_of 会取 max）
            "tool_calls": int(row.tool_calls or 0),
            "payload": row.payload or {},
            "created_at": row.created_at,
        }
        for row in session.scalars(stmt)
    ]


def filter_rows_for_departments(rows: list[dict], departments: list[str] | None) -> list[dict]:
    """按部门过滤轮次行（None=全量/管理员）；账号→部门用演示账号表，未知账号视为不可见。"""
    if departments is None:
        return rows
    allowed = set(departments)
    names = [row for row in rows if department_of_user(row.get("username") or "") in allowed]
    return names


def agent_turn_stats(session: Session, *, hours: int = 24,
                     departments: list[str] | None = None) -> dict:
    """按 Agent 聚合最近 N 轮的轮次/延迟/成本/降级（per-agent 看板的数据源）。

    `departments` 为 None 表示全量（管理员）；部门用户只看本部门账号产生的轮次。
    延迟分位用「排序取分位」实现：SQLite 与 MySQL 的窗口函数支持差异大，
    而轮次量级（演示 1e3、生产 1e5/天）足够在一次查询里排完。
    """
    cutoff = dt.datetime.now() - dt.timedelta(hours=max(1, int(hours)))
    stmt = select(ChatTurnLog).where(ChatTurnLog.created_at >= cutoff)
    rows = list(session.scalars(stmt))
    if departments is not None:
        allowed = set(departments)
        names = {
            row.username for row in rows
            if department_of_user(row.username) in allowed
        }
        rows = [row for row in rows if row.username in names]

    buckets: dict[str, dict] = {}
    for row in rows:
        key = row.agent_id or "(unrouted)"
        item = buckets.setdefault(key, {
            "turns": 0, "errors": 0, "fallbacks": 0, "tool_calls": 0,
            "tokens_in": 0, "tokens_out": 0, "cost_cny": 0.0,
            "grounding": {}, "latencies": [], "versions": set(),
        })
        item["turns"] += 1
        item["tool_calls"] += int(row.tool_calls or 0)
        item["tokens_in"] += int(row.tokens_in or 0)
        item["tokens_out"] += int(row.tokens_out or 0)
        item["cost_cny"] += float(row.cost_cny or 0.0)
        item["latencies"].append(int(row.elapsed_ms or 0))
        if row.fallback_used:
            item["fallbacks"] += 1
        if row.grounding_status:
            item["grounding"][row.grounding_status] = \
                item["grounding"].get(row.grounding_status, 0) + 1
        if row.agent_version:
            item["versions"].add(row.agent_version)
        if not row.elapsed_ms:
            item["errors"] += 1

    out: dict[str, dict] = {}
    for agent_id, item in sorted(buckets.items()):
        ordered = sorted(item["latencies"])
        turns = max(1, item["turns"])
        grounding_total = sum(item["grounding"].values()) or 1
        out[agent_id] = {
            "turns": item["turns"],
            "tool_calls": item["tool_calls"],
            "p50_ms": _percentile(ordered, 0.50),
            "p95_ms": _percentile(ordered, 0.95),
            "tokens_in": item["tokens_in"],
            "tokens_out": item["tokens_out"],
            "cost_cny": round(item["cost_cny"], 4),
            "avg_tools_per_turn": round(item["tool_calls"] / turns, 2),
            "fallback_rate": round(item["fallbacks"] / turns, 4),
            "grounding": item["grounding"],
            "grounding_pass_rate": round(
                item["grounding"].get("passed", 0) / grounding_total, 4),
            "versions": sorted(item["versions"]),
        }
    return {"window_hours": hours, "agents": out, "total_turns": len(rows),
            "note": "按 agent_id 聚合；errors=缺少耗时的轮次（多为异常中断）"}


def _percentile(ordered: list[int], ratio: float) -> int:
    if not ordered:
        return 0
    import math

    idx = max(0, math.ceil(ratio * len(ordered)) - 1)
    return int(ordered[idx])


def department_of_user(username: str) -> str | None:
    """账号 → 部门（per-agent 看板做部门隔离时用；未知账号返回 None）。"""
    from app.auth import DEMO_ACCOUNTS

    spec = DEMO_ACCOUNTS.get(username) or {}
    return spec.get("department")


def prune_turn_logs(session: Session, session_id: str, keep: int = TURN_LOG_KEEP) -> int:
    """保留最近 keep 轮，删除更早的运行详情。"""
    rows = list(session.scalars(
        select(ChatTurnLog.id).where(ChatTurnLog.session_id == session_id)
        .order_by(ChatTurnLog.id.desc())
    ))
    stale = rows[keep:]
    if stale:
        session.execute(delete(ChatTurnLog).where(ChatTurnLog.id.in_(stale)))
    return len(stale)


def get_turn_logs(session: Session, session_id: str) -> dict[int, dict]:
    """message_id -> run_detail（供历史消息回看当时的运行详情与事实卡）。"""
    rows = session.scalars(
        select(ChatTurnLog).where(ChatTurnLog.session_id == session_id)
        .order_by(ChatTurnLog.id)
    ).all()
    return {r.message_id: (r.payload or {}) for r in rows if r.message_id}


def count_turn_logs(session: Session, session_id: str) -> int:
    return int(session.scalar(
        select(func.count()).select_from(ChatTurnLog).where(ChatTurnLog.session_id == session_id)
    ) or 0)
