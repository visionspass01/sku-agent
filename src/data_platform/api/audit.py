"""中台侧调用审计（审计下沉）。

每条中台调用都落一行 `api_audit_log`：谁（agent_id）、要什么能力、结果、被拒原因、耗时、请求 id。
多 Agent 场景下这是**唯一的可信证据链**——Agent 侧日志只能证明"我以为我调了"，
中台的审计行才能证明"它确实调了、且我允许/拒绝了它"。

写入是"响应之后一次 insert"：SQLite WAL 下毫秒级；行数超过 `PLATFORM_AUDIT_KEEP` 时批量删最旧，
避免无限增长（也可以交给外部日志管道，这里保持零依赖）。
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import delete, func, select

from data_platform import config
from data_platform.db import ApiAuditLog, session_scope

logger = logging.getLogger("data-platform.audit")

# 允许的决策值（写进 DB 前收敛，避免脏值让看板/统计出问题）
DECISIONS = frozenset({
    "allow", "unauthenticated", "denied_scope", "denied_department",
    "rate_limited", "not_found", "conflict", "error",
})


def record(*, agent_id: str, scopes, method: str, path: str, status: int, latency_ms: int,
           request_id: str = "", decision: str = "allow", detail: str = "") -> None:
    """写一行审计。失败只告警：审计不能反向影响业务可用性。"""
    if decision not in DECISIONS:
        decision = "error"
    try:
        with session_scope() as session:
            session.add(ApiAuditLog(
                at=dt.datetime.now(), agent_id=agent_id or "", scopes=",".join(sorted(scopes or [])),
                method=method[:8], path=path[:160], status=int(status), latency_ms=int(latency_ms),
                request_id=request_id[:64], decision=decision, detail=(detail or "")[:200],
            ))
            _prune(session)
    except Exception as exc:  # noqa: BLE001 —— 审计失败不能影响响应
        logger.warning("写审计失败：%s", exc)


def _prune(session) -> None:  # noqa: ANN001
    keep = max(100, int(config.AUDIT_KEEP))
    total = int(session.scalar(select(func.count()).select_from(ApiAuditLog)) or 0)
    if total <= keep:
        return
    rows = list(session.scalars(
        select(ApiAuditLog.id).order_by(ApiAuditLog.id.desc()).limit(total - keep)
    ))
    if rows:
        session.execute(delete(ApiAuditLog).where(ApiAuditLog.id.in_(rows)))


def recent(limit: int = 50, agent_id: str = "") -> list[dict]:
    """最近的审计行（`/api/v1/audit` 用）：新的在前。"""
    limit = max(1, min(int(limit or 50), 200))
    with session_scope() as session:
        stmt = select(ApiAuditLog).order_by(ApiAuditLog.id.desc()).limit(limit)
        if agent_id:
            stmt = stmt.where(ApiAuditLog.agent_id == agent_id)
        rows = session.scalars(stmt).all()
        return [{
            "at": r.at.isoformat(timespec="seconds"),
            "agent_id": r.agent_id, "scopes": [s for s in (r.scopes or "").split(",") if s],
            "method": r.method, "path": r.path, "status": r.status,
            "latency_ms": r.latency_ms, "request_id": r.request_id,
            "decision": r.decision, "detail": r.detail,
        } for r in rows]
