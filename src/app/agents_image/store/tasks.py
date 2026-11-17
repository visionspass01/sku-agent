"""任务表读写：创建 / 查询 / 状态流转 / 重启后的超时恢复。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from app.agents_image.store.models import ImageTask


def create_task(session, *, task_id: str, username: str, department: str | None,
                prompt: str, platform: str, aspect: str, n: int,
                ref_asset_ids: list[str], compositions: list[str] | None = None,
                scene_styles: list[str] | None = None, slots: list[dict] | None = None,
                prompt_pack: list[dict] | None = None, request_id: str = "",
                trace_id: str = "") -> ImageTask:
    task = ImageTask(
        task_id=task_id, request_id=request_id, username=username, department=department,
        prompt=prompt, platform=platform, aspect=aspect, n=n,
        ref_asset_ids=list(ref_asset_ids or []),
        compositions=list(compositions or []), scene_styles=list(scene_styles or []),
        slots_json=list(slots or []), prompt_pack_json=list(prompt_pack or []),
        status="queued", trace_id=trace_id,
    )
    session.add(task)
    session.flush()
    return task


def get_task(session, task_id: str) -> ImageTask | None:
    return session.get(ImageTask, task_id)


def update_task(session, task_id: str, **fields: Any) -> ImageTask | None:
    task = session.get(ImageTask, task_id)
    if task is None:
        return None
    for key, value in fields.items():
        if hasattr(task, key):
            setattr(task, key, value)
    session.flush()
    return task


def mark_running(session, task_id: str) -> None:
    update_task(session, task_id, status="running", started_at=dt.datetime.now())


def mark_done(session, task_id: str, *, attempted: int, delivered: int, cost: float,
              prompt_final: str = "", prompt_pack: list[dict] | None = None,
              slots: list[dict] | None = None) -> None:
    update_task(session, task_id, status="done", finished_at=dt.datetime.now(),
                attempted_images=attempted, delivered_images=delivered, cost_cny=cost,
                prompt_final=prompt_final,
                prompt_pack_json=list(prompt_pack or []), slots_json=list(slots or []))


def mark_failed(session, task_id: str, *, code: str, detail: str = "") -> None:
    update_task(session, task_id, status="failed", finished_at=dt.datetime.now(),
                error_code=code, error_detail=detail)


def list_tasks(session, *, username: str | None = None, limit: int = 50,
               status: str | None = None) -> list[ImageTask]:
    """按创建时间倒序列任务；`username=None` 表示不限（管理员视角）。"""
    statement = select(ImageTask)
    if username:
        statement = statement.where(ImageTask.username == username)
    if status:
        statement = statement.where(ImageTask.status == status)
    statement = statement.order_by(ImageTask.created_at.desc()).limit(max(1, int(limit)))
    return list(session.execute(statement).scalars().all())


def list_recoverable(session, *, timeout_seconds: int) -> list[ImageTask]:
    """重启后需要续跑的任务：`queued`，或 `running` 但已超时（进程被杀时留下的）。"""
    cutoff = dt.datetime.now() - dt.timedelta(seconds=timeout_seconds)
    rows = session.execute(
        select(ImageTask).where(ImageTask.status.in_(("queued", "running")))
    ).scalars().all()
    return [row for row in rows
            if row.status == "queued" or (row.started_at is None or row.started_at < cutoff)]
