"""回写中台的补偿队列（outbox）：失败进队 → 可重放 → 成功后标记 sent。

为什么不"重试到成功为止"：回写失败最常见原因是中台短时不可用，
阻塞执行体会拖垮并发；先落 outbox，由 `/api/image/admin/replay-outbox` 或定时任务重放。

第四轮 P1-5：重放改成**三段式**，网络 IO 绝不放在数据库会话里（SQLite 下会拿着写锁等网络，
线上表现为 `database is locked`）：
    1. `pending_payloads()` —— 一个短会话读出到期行的纯数据后立刻关闭；
    2. `claim(ids, seconds)` —— 一个短会话把 `next_retry_at` 推后，防并发/重复重发；
    3. `await save_result(...)` —— 纯网络，**不持有任何会话**；
    4. `mark_sent_by_id()` / `mark_failed_by_id()` —— 各自一个短事务收尾。
P2-7：`prune()` 清理过老的 sent 行，`stats()` 用 GROUP BY 聚合（不再全表读进内存）。
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import delete, func, select, update

from app.agents_image.store.models import ImageOutbox

logger = logging.getLogger("sku-agent.image.outbox")

MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 30
# claim 占位时长：重放期间把 next_retry_at 推后这么久，避免并发重放同一批（可被配置覆盖）
CLAIM_SECONDS = 120
DEFAULT_KEEP_HOURS = 72


def _session():
    """短会话工厂（延迟导入，避免 store 层与 db 引擎在 import 期互相拉扯）。"""
    from app.db.engine import session_scope

    return session_scope()


def enqueue(session, *, task_id: str, payload: dict, error: str = "") -> ImageOutbox:
    row = ImageOutbox(task_id=task_id, payload_json=payload or {},
                      status="pending", attempts=0,
                      next_retry_at=dt.datetime.now() + dt.timedelta(seconds=BASE_BACKOFF_SECONDS),
                      last_error=(error or "")[:500], created_at=dt.datetime.now())
    session.add(row)
    session.flush()
    return row


def _due_rows(session, *, limit: int = 20, now: dt.datetime | None = None) -> list[ImageOutbox]:
    moment = now or dt.datetime.now()
    rows = session.execute(
        select(ImageOutbox).where(ImageOutbox.status == "pending")
        .order_by(ImageOutbox.created_at).limit(max(1, int(limit)))
    ).scalars().all()
    return [row for row in rows
            if row.next_retry_at is None or row.next_retry_at <= moment]


def pending_payloads(*, limit: int = 20, now: dt.datetime | None = None) -> list[dict]:
    """读出到期的 pending 行（**纯数据 + 短会话**，调用方拿到的对象不绑会话）。"""
    with _session() as session:
        return [{"id": int(row.id), "task_id": row.task_id,
                 "payload": dict(row.payload_json or {})}
                for row in _due_rows(session, limit=limit, now=now)]


def claim(ids, *, seconds: int | None = None, now: dt.datetime | None = None) -> list[int]:
    """把即将重放的行推后一个占位时长（防并发送/重发）；返回真正占到的 id 列表。"""
    from app.agents_image.config import get_image_settings

    clean = [int(item) for item in (ids or [])]
    if not clean:
        return []
    seconds = int(seconds if seconds is not None else
                  getattr(get_image_settings(), "outbox_claim_seconds", CLAIM_SECONDS))
    moment = (now or dt.datetime.now()) + dt.timedelta(seconds=max(1, seconds))
    with _session() as session:
        session.execute(
            update(ImageOutbox)
            .where(ImageOutbox.id.in_(clean), ImageOutbox.status == "pending")
            .values(next_retry_at=moment)
        )
        session.flush()
        # 回读真正占到占位时长的行（并发下别人可能刚把它标成 sent/failed）
        claimed = [int(row) for row in session.execute(
            select(ImageOutbox.id)
            .where(ImageOutbox.id.in_(clean), ImageOutbox.status == "pending",
                   ImageOutbox.next_retry_at == moment)
        ).scalars().all()]
    return claimed


def mark_sent(session, row: ImageOutbox) -> None:
    row.status = "sent"
    row.attempts = int(row.attempts or 0) + 1
    row.last_error = ""
    session.flush()


def mark_failed(session, row: ImageOutbox, error: str) -> None:
    row.attempts = int(row.attempts or 0) + 1
    row.last_error = (error or "")[:500]
    if row.attempts >= MAX_ATTEMPTS:
        row.status = "failed"
        row.next_retry_at = None
    else:
        delay = BASE_BACKOFF_SECONDS * (2 ** (row.attempts - 1))
        row.next_retry_at = dt.datetime.now() + dt.timedelta(seconds=delay)
    session.flush()


def mark_sent_by_id(row_id: int) -> bool:
    """按 id 标记 sent（各自一个短事务；行已被别处处理则返回 False）。"""
    with _session() as session:
        row = session.get(ImageOutbox, int(row_id))
        if row is None:
            return False
        mark_sent(session, row)
        return True


def mark_failed_by_id(row_id: int, error: str) -> bool:
    with _session() as session:
        row = session.get(ImageOutbox, int(row_id))
        if row is None:
            return False
        mark_failed(session, row, error)
        return True


async def replay(*, catalog=None, limit: int = 20, now: dt.datetime | None = None) -> dict:
    """把到期的 pending 记录重新回写中台；返回 {sent, failed, skipped, claimed}。

    网络调用（`save_result`）发生在**所有会话之外**（P1-5）。
    """
    from app.agents_image.sources.product_source import get_catalog

    target = catalog or get_catalog()
    rows = pending_payloads(limit=limit, now=now)
    if not rows:
        return {"sent": 0, "failed": 0, "skipped": 0, "claimed": 0}
    claimed = claim([row["id"] for row in rows], now=now)
    sent = failed = 0
    for row in rows:
        try:
            await target.save_result(dict(row["payload"] or {}))
            mark_sent_by_id(row["id"])
            sent += 1
        except Exception as exc:  # noqa: BLE001 —— 单条失败不影响其它条
            from app.agents_image import metrics as image_metrics

            image_metrics.record_swallowed("outbox_replay")
            logger.warning("outbox 重放失败：task=%s %s", row["task_id"], exc)
            mark_failed_by_id(row["id"], f"{type(exc).__name__}: {exc}")
            failed += 1
    return {"sent": sent, "failed": failed, "skipped": len(rows) - sent - failed,
            "claimed": len(claimed)}


def prune(*, keep_hours: int | None = None, now: dt.datetime | None = None) -> int:
    """清理已经 `sent` 且超过保留期的行（P2-7：过去 sent 行永不清理）。返回删除条数。"""
    from app.agents_image.config import get_image_settings

    hours = int(keep_hours if keep_hours is not None else
                getattr(get_image_settings(), "outbox_keep_hours", DEFAULT_KEEP_HOURS))
    cutoff = (now or dt.datetime.now()) - dt.timedelta(hours=max(0, hours))
    with _session() as session:
        result = session.execute(
            delete(ImageOutbox).where(ImageOutbox.status == "sent",
                                      ImageOutbox.created_at < cutoff))
        removed = int(result.rowcount or 0)
    if removed:
        logger.info("outbox 清理：删除 %s 条已回写记录（保留 %s 小时）", removed, hours)
    return removed


def list_outbox(session, *, status: str | None = None, limit: int = 20) -> list[dict]:
    statement = select(ImageOutbox).order_by(ImageOutbox.created_at.desc()).limit(
        max(1, int(limit)))
    if status:
        statement = statement.where(ImageOutbox.status == status)
    rows = session.execute(statement).scalars().all()
    return [{"id": row.id, "task_id": row.task_id, "status": row.status,
             "attempts": int(row.attempts or 0), "last_error": row.last_error,
             "next_retry_at": row.next_retry_at.isoformat() if row.next_retry_at else "",
             "created_at": row.created_at.isoformat() if row.created_at else ""}
            for row in rows]


def stats() -> dict:
    """按状态聚合（GROUP BY，不再把整表读进内存）。"""
    with _session() as session:
        rows = session.execute(
            select(ImageOutbox.status, func.count()).group_by(ImageOutbox.status)
        ).all()
    counts = {"pending": 0, "sent": 0, "failed": 0}
    for status, count in rows:
        counts[str(status or "unknown")] = int(count)
    return counts


__all__ = ["MAX_ATTEMPTS", "CLAIM_SECONDS", "enqueue", "pending_payloads", "claim",
           "mark_sent", "mark_failed", "mark_sent_by_id", "mark_failed_by_id",
           "replay", "prune", "list_outbox", "stats"]
