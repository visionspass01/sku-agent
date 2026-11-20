"""任务表读写：创建 / 查询 / 状态流转 / 重启后的超时恢复。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, update

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
                reused_images: int = 0,
                warnings: list[str] | None = None) -> ImageTask:
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
        warnings_json=list(warnings or []),
        status="queued", trace_id=trace_id,
    )
    session.add(task)
    session.flush()
    return task


def create_task_with_retry(session, *, id_factory, max_tries: int = 5, **fields) -> ImageTask:
    """建任务并**兜住主键撞车**（P1-6，第十二轮）。

    任务 ID 是"时间前缀 + 4 位随机 hex"：同一秒内最多 65536 个名额，而一次批量请求
    最多可建 60 个任务（几乎全在同一秒），撞主键概率约 2.7%/批。过去撞了就是
    `IntegrityError` 冒到路由层 → **整批回滚 + 500**（同一事务里先建的也一起丢）。
    这里用 SAVEPOINT 只回滚这一条 INSERT，换一个 ID 重试；重试仍失败才抛。
    """
    from sqlalchemy.exc import IntegrityError

    last_exc: BaseException | None = None
    for _ in range(max(1, int(max_tries))):
        task_id = id_factory()
        try:
            with session.begin_nested():        # SAVEPOINT：只回滚这一条
                return create_task(session, task_id=task_id, **fields)
        except IntegrityError as exc:           # 主键撞车（极小概率）→ 换 id 再来
            last_exc = exc
            continue
    raise last_exc if last_exc is not None else RuntimeError("建任务失败")


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


def mark_running(session, task_id: str) -> bool:
    """把任务置为 running（**CAS**：只有 queued 才能变成 running）。

    P1-5（第十二轮）：上一版是"先读状态、再无条件写"，与取消/失败之间有一个窗口：
    用户在这中间取消，`update_task` 会把 `canceled` 覆盖回 `running`——界面显示已取消，
    任务却继续出图、继续计费，最后连状态也变成 done。这里用条件 UPDATE 抢执行权，
    抢不到（已被取消/已结算/已被别的执行体认领）就返回 False，调用方直接放弃执行。
    """
    result = session.execute(
        update(ImageTask)
        .where(ImageTask.task_id == task_id, ImageTask.status == "queued")
        .values(status="running", started_at=dt.datetime.now())
    )
    session.flush()
    return bool(result.rowcount)


def merge_warnings(session, task_id: str, warnings: list[str] | None) -> list[str]:
    """把新告警**合并**进 `warnings_json`（去重保序），返回合并后的列表。

    建任务阶段就可能带告警（例如 iterate 命中不了复用的 `IMG_REUSE_SKIPPED`），
    结算/回写阶段如果直接覆盖，这些"为什么没复用/为什么少图"的说明会在任务完成那一刻
    凭空消失（第四轮 P1-3 实测）。所以所有"补告警"的地方统一走这里。
    """
    task = get_task(session, task_id)
    if task is None:
        return []
    if warnings is None:
        return list(getattr(task, "warnings_json", None) or [])
    merged: list[str] = []
    for item in [*list(getattr(task, "warnings_json", None) or []), *warnings]:
        text = str(item)
        if text and text not in merged:
            merged.append(text)
    update_task(session, task_id, warnings_json=merged)
    return merged


def mark_done(session, task_id: str, *, attempted: int, delivered: int, cost: float,
              prompt_final: str = "", prompt_pack: list[dict] | None = None,
              slots: list[dict] | None = None, backfill_status: str | None = None,
              attempts: int | None = None, external_cost: float | None = None,
              llm_cost: float | None = None) -> None:
    fields: dict = dict(status="done", finished_at=dt.datetime.now(),
                        attempted_images=attempted, delivered_images=delivered, cost_cny=cost,
                        prompt_final=prompt_final,
                        prompt_pack_json=list(prompt_pack or []),
                        slots_json=list(slots or []),
                        lease_until=None)   # 终态释放认领，避免看门狗/重试被 claim 拦住
    if external_cost is not None:
        fields["external_cost_cny"] = float(external_cost)
    if llm_cost is not None:
        fields["llm_cost_cny"] = float(llm_cost)
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


def mark_failed(session, task_id: str, *, code: str, detail: str = "") -> bool:
    """把任务置为 failed（**CAS**：不覆盖已取消/已交付的终态）。

    P1-5（第十二轮）：失败落库同样不能无条件写——用户取消后，后台执行体抛出的异常
    会把 `canceled` 覆盖成 `failed`（结算被 `settled_at` 拦住，但终态语义已经被改写）。
    终态要释放认领（lease），否则"失败后立刻重试/重投"会被 claim 拦住。
    """
    result = session.execute(
        update(ImageTask)
        .where(ImageTask.task_id == task_id,
               ImageTask.status.not_in(("canceled", "done")))
        .values(status="failed", finished_at=dt.datetime.now(),
                error_code=code, error_detail=detail, lease_until=None)
    )
    session.flush()
    return bool(result.rowcount)


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


RETRYABLE_STATUSES = ("failed", "canceled")


def begin_retry(session, task_id: str, *, retries: int) -> bool:
    """抢占「重试权」并把任务重置成干净的 queued（P1-2，第九轮）。

    为什么必须用**条件 UPDATE**：上一版是「读 status → 判断 → reserve → reset」，
    两个并发重试请求都能读到 `failed` 并各自 reserve 一份额度；只有一次 claim 会成功，
    多占的那份额度没有任何路径释放（settle_done 只释放 `planned - charged`），
    直到周期 rollover 才消失——表现为"配额莫名其妙被占住"，同时 retries 被 +2。

    现在：`UPDATE ... WHERE status IN ('failed','canceled') AND retries = :retries`
    抢占重试权，成功者才继续 reserve；reserve 失败时整个事务回滚（CAS 一起撤销），
    不会留下"已重置但没预留"的半账。
    """
    result = session.execute(
        update(ImageTask)
        .where(ImageTask.task_id == task_id,
               ImageTask.status.in_(RETRYABLE_STATUSES),
               ImageTask.retries == int(retries))
        .values(status="queued", error_code="", error_detail="", finished_at=None,
                started_at=None, attempts=0, attempted_images=0, delivered_images=0,
                provider_started_at=None, cancel_requested=False, lease_until=None,
                recoveries=0, settled_at=None, provider_calls=0,
                warnings_json=[], blocked_json=[], reused_images=0,
                copy_json={}, prompt_pack_json=[], listing_status="not_listed",
                listed_at=None, listing_url="", retries=int(retries) + 1)
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


@dataclass(frozen=True)
class RecoveryCandidate:
    """看门狗候选：`queued`（从未被认领）或 `running`（租约过期）。"""

    task_id: str
    kind: str          # queued | running


def list_recoverable(session, *, lease_seconds: int = 300, grace_seconds: int = 30) -> list[RecoveryCandidate]:
    """需要续跑的候选：**未被认领的 queued**，或 **租约过期的 running**。

    第三轮 P0-2 的关键：把"排队中"和"死掉了"分开。
    - `queued` 只有在**没有认领标记（lease）且已经超过 grace** 时才算候选：刚建出来的任务
      正在被创建请求 `submit()`，看门狗抢跑会把它算成一次"重投"（过去就是这么误杀的）；
    - `running` 只认租约过期，正常在途任务（每阶段续约）永远不会被重投。
    """
    moment = dt.datetime.now()
    lease_cutoff = moment - dt.timedelta(seconds=max(30, int(lease_seconds or 300)))
    grace_cutoff = moment - dt.timedelta(seconds=max(0, int(grace_seconds or 0)))
    rows = session.execute(
        select(ImageTask).where(ImageTask.status.in_(("queued", "running")))
    ).scalars().all()
    out: list[RecoveryCandidate] = []
    for row in rows:
        lease = getattr(row, "lease_until", None)
        if row.status == "queued":
            if lease is not None and lease >= moment:
                continue                       # 已被某个实例/请求认领，正在排队或执行
            if (row.created_at or dt.datetime.min) >= grace_cutoff:
                continue                       # 刚建出来，给创建请求一点时间提交
            out.append(RecoveryCandidate(task_id=row.task_id, kind="queued"))
            continue
        if lease is None:
            # 老任务没有租约：退回"启动时间 + 租约时长"判断
            if (row.started_at or dt.datetime.min) < lease_cutoff:
                out.append(RecoveryCandidate(task_id=row.task_id, kind="running"))
            continue
        if lease < moment:
            out.append(RecoveryCandidate(task_id=row.task_id, kind="running"))
    return out


def claim(session, task_id: str, *, lease_seconds: int = 300) -> bool:
    """认领一个**排队中**任务（CAS）：只有 queued 且租约空/过期才成功。

    用条件 UPDATE 而不是先查后改：多实例/看门狗并发认领同一任务时只有一个能拿到，
    另一个直接放弃——这是"同一任务不被执行两次"的兜底。
    """
    now = dt.datetime.now()
    result = session.execute(
        update(ImageTask)
        .where(ImageTask.task_id == task_id,
               ImageTask.status == "queued",
               or_(ImageTask.lease_until.is_(None), ImageTask.lease_until < now))
        .values(lease_until=now + dt.timedelta(seconds=max(30, int(lease_seconds))))
    )
    session.flush()
    return bool(result.rowcount)


def claim_running(session, task_id: str, *, lease_seconds: int = 300) -> bool:
    """接管一个**租约过期**的 running（看门狗重投）：同样用 CAS 防止两个实例同时接管。"""
    now = dt.datetime.now()
    result = session.execute(
        update(ImageTask)
        .where(ImageTask.task_id == task_id,
               ImageTask.status == "running",
               or_(ImageTask.lease_until.is_(None), ImageTask.lease_until < now))
        .values(lease_until=now + dt.timedelta(seconds=max(30, int(lease_seconds))),
                status="queued")
    )
    session.flush()
    return bool(result.rowcount)


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
