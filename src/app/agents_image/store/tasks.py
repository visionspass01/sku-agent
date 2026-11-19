"""任务表读写：创建 / 查询 / 状态流转 / 重启后的超时恢复。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select, update

from app.agents_image.store.models import ImageTask


def create_task(session, *, task_id: str, username: str, department: str | None,
                prompt: str, platform: str, aspect: str, n: int,
                ref_asset_ids: list[str], compositions: list[str] | None = None,
                scene_styles: list[str] | None = None, slots: list[dict] | None = None,
                prompt_pack: list[dict] | None = None, request_id: str = "",
                trace_id: str = "", batch_id: str = "", product_id: str | None = None,
                ref_source: str = "mcp", attrs: dict | None = None,
                color_key: str = "", variant_id: str = "",
                image_count: int | None = None, slot_count: int | None = None,
                copy: dict | None = None, base_task_id: str | None = None,
                reuse_asset_ids: dict | None = None,
                reused_images: int = 0) -> ImageTask:
    task = ImageTask(
        task_id=task_id, request_id=request_id, batch_id=(batch_id or None),
        username=username, department=department, product_id=product_id,
        ref_source=ref_source,
        prompt=prompt, platform=platform, aspect=aspect, n=n,
        ref_asset_ids=list(ref_asset_ids or []),
        color_key=color_key or "", variant_id=variant_id or "",
        compositions=list(compositions or []), scene_styles=list(scene_styles or []),
        attrs_json=dict(attrs or {}), copy_json=dict(copy or {}),
        base_task_id=base_task_id or None,
        reuse_asset_ids=dict(reuse_asset_ids or {}),
        reused_images=int(reused_images or 0),
        slot_count=int(slot_count if slot_count is not None else len(slots or [])),
        image_count=int(image_count if image_count is not None else n),
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
              slots: list[dict] | None = None, backfill_status: str | None = None,
              attempts: int | None = None, external_cost: float | None = None) -> None:
    fields: dict = dict(status="done", finished_at=dt.datetime.now(),
                        attempted_images=attempted, delivered_images=delivered, cost_cny=cost,
                        prompt_final=prompt_final,
                        prompt_pack_json=list(prompt_pack or []),
                        slots_json=list(slots or []))
    if external_cost is not None:
        fields["external_cost_cny"] = float(external_cost)
    if backfill_status:
        fields["backfill_status"] = backfill_status
    if attempts is not None:
        fields["attempts"] = int(attempts)
    update_task(session, task_id, **fields)


LISTING_STATUSES = ("not_listed", "pending", "listed", "failed")


def update_listing(session, task_id: str, *, status: str, url: str = "") -> ImageTask | None:
    """记录上架状态（B2）：`listed` 时写 `listed_at`，回到 `not_listed` 时清空。"""
    if status not in LISTING_STATUSES:
        from app.core.errors import raise_image

        raise_image("IMG_INPUT_INVALID",
                    f"不支持的上架状态：{status}（可选 {'、'.join(LISTING_STATUSES)}）")
    fields: dict = {"listing_status": status, "listing_url": (url or "").strip()[:500]}
    if status == "listed":
        fields["listed_at"] = dt.datetime.now()
    elif status == "not_listed":
        fields["listed_at"] = None
    return update_task(session, task_id, **fields)


def mark_backfill(session, task_id: str, status: str) -> None:
    """回写中台的状态（pending/sent/failed）单独更新：图片交付与回写解耦。"""
    update_task(session, task_id, backfill_status=status)


def mark_failed(session, task_id: str, *, code: str, detail: str = "") -> None:
    update_task(session, task_id, status="failed", finished_at=dt.datetime.now(),
                error_code=code, error_detail=detail)


def mark_settled(session, task_id: str) -> bool:
    """抢占「结算权」（第三轮 P0-1）：`settled_at` 从 NULL 变成现在才返回 True。

    用条件 UPDATE 而不是"先查后写"：并发下（取消 vs 失败 vs 交付）只有一个路径能结算，
    避免同一笔预留被释放两次（第二次释放会把**别人**的预留吃掉）。
    """
    result = session.execute(
        update(ImageTask)
        .where(ImageTask.task_id == task_id, ImageTask.settled_at.is_(None))
        .values(settled_at=dt.datetime.now())
    )
    session.flush()
    return bool(result.rowcount)


def list_tasks(session, *, username: str | None = None, limit: int = 50,
               status: str | None = None, batch_id: str | None = None,
               offset: int = 0, department: str | None = None) -> list[ImageTask]:
    """按创建时间倒序列任务；`username=None` 不限（管理员视角），可按批次/部门过滤。"""
    statement = select(ImageTask)
    if username:
        statement = statement.where(ImageTask.username == username)
    if status:
        statement = statement.where(ImageTask.status == status)
    if batch_id:
        statement = statement.where(ImageTask.batch_id == batch_id)
    if department:
        statement = statement.where(ImageTask.department == department)
    statement = statement.order_by(ImageTask.created_at.desc())
    statement = statement.limit(max(1, int(limit))).offset(max(0, int(offset)))
    return list(session.execute(statement).scalars().all())


def list_recoverable(session, *, timeout_seconds: int = 0,
                     lease_seconds: int | None = None,
                     max_recoveries: int = 3) -> list[ImageTask]:
    """需要续跑的任务（W1）：`queued`，或 **租约已过期** 的 `running`。

    只认租约（`lease_until`）而不是"启动超过 N 秒"：真实 provider 单任务可能跑几分钟，
    用固定超时会把**在途正常任务**当成死任务重投 → 同一任务被执行两次、成本翻倍。
    重投次数封顶由调用方处理（超限判失败），这里不再按 `recoveries` 过滤——
    否则到了上限的任务会永远停在 `running`，既不重投也不失败。
    # noqa: ARG002 —— `max_recoveries` 保留给调用方语义对齐
    """
    moment = dt.datetime.now()
    span = lease_seconds if lease_seconds is not None else timeout_seconds
    cutoff = moment - dt.timedelta(seconds=max(30, int(span or 300)))
    rows = session.execute(
        select(ImageTask).where(ImageTask.status.in_(("queued", "running")))
    ).scalars().all()
    out: list[ImageTask] = []
    for row in rows:
        if row.status == "queued":
            out.append(row)
            continue
        lease = getattr(row, "lease_until", None)
        if lease is None:
            # 老任务没有租约：退回"启动时间 + 租约时长"判断
            if (row.started_at or dt.datetime.min) < cutoff:
                out.append(row)
            continue
        if lease < moment:
            out.append(row)
    return out


def renew_lease(session, task_id: str, *, seconds: int) -> None:
    """续约（执行期心跳）：避免看门狗把在途任务当成死任务重投。"""
    update_task(session, task_id,
                lease_until=dt.datetime.now() + dt.timedelta(seconds=max(30, int(seconds))))


def bump_recoveries(session, task_id: str) -> int:
    """被看门狗重投时累加计数，返回累加后的值（超限即判失败）。"""
    task = session.get(ImageTask, task_id)
    if task is None:
        return 0
    task.recoveries = int(task.recoveries or 0) + 1
    session.flush()
    return task.recoveries
