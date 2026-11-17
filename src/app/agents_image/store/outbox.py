"""回写中台的补偿队列（outbox）：失败进队 → 可重放 → 成功后标记 sent。

为什么不"重试到成功为止"：回写失败最常见原因是中台短时不可用，
阻塞执行体会拖垮并发；先落 outbox，由 `/api/image/admin/replay-outbox` 或定时任务重放。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.agents_image.store.models import ImageOutbox

MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 30


def enqueue(session, *, task_id: str, payload: dict, error: str = "") -> ImageOutbox:
    row = ImageOutbox(task_id=task_id, payload_json=payload or {},
                      status="pending", attempts=0,
                      next_retry_at=dt.datetime.now() + dt.timedelta(seconds=BASE_BACKOFF_SECONDS),
                      last_error=(error or "")[:500], created_at=dt.datetime.now())
    session.add(row)
    session.flush()
    return row


def pending(session, *, limit: int = 20, now: dt.datetime | None = None) -> list[ImageOutbox]:
    moment = now or dt.datetime.now()
    rows = session.execute(
        select(ImageOutbox).where(ImageOutbox.status == "pending")
        .order_by(ImageOutbox.created_at).limit(max(1, int(limit)))
    ).scalars().all()
    return [row for row in rows
            if row.next_retry_at is None or row.next_retry_at <= moment]


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


async def replay(session, *, catalog=None, limit: int = 20,
                 now: dt.datetime | None = None) -> dict:
    """把到期的 pending 记录重新回写中台；返回 {sent, failed, skipped}。"""
    from app.agents_image.sources.product_source import get_catalog

    target = catalog or get_catalog()
    rows = pending(session, limit=limit, now=now)
    sent = failed = 0
    for row in rows:
        try:
            await target.save_result(dict(row.payload_json or {}))
            mark_sent(session, row)
            sent += 1
        except Exception as exc:  # noqa: BLE001 —— 单条失败不影响其它条
            mark_failed(session, row, f"{type(exc).__name__}: {exc}")
            failed += 1
    return {"sent": sent, "failed": failed, "skipped": len(rows) - sent - failed}


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


def stats(session) -> dict:
    rows = session.execute(select(ImageOutbox)).scalars().all()
    counts = {"pending": 0, "sent": 0, "failed": 0}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts


__all__ = ["MAX_ATTEMPTS", "enqueue", "pending", "mark_sent", "mark_failed", "replay",
           "list_outbox", "stats"]
