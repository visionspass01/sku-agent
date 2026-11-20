"""本地调度器：进程内 asyncio 任务 + 信号量（开发/测试默认），状态全在 DB。

- `submit(task_id)`：起一个 asyncio 任务去跑 `graph.execute_task`；
- `resume_pending()`：启动/重启后把 `queued` 或超时 `running` 的任务重新提交（幂等）；
- 取消：DB 标记 `canceled`，执行体在关键点检查。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from app.agents_image.config import get_image_settings
from app.agents_image.store import tasks as task_store
from app.db.engine import session_scope

logger = logging.getLogger("sku-agent.image.dispatch")


class LocalGenerator:
    name = "local"

    def __init__(self) -> None:
        self.settings = get_image_settings()
        self._semaphore = asyncio.Semaphore(max(1, self.settings.concurrency))
        self._tasks: dict[str, asyncio.Task] = {}
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def submit(self, task_id: str, *, force: bool = False) -> str:
        """提交任务：先 CAS **认领**（`claim`），认领失败说明别人已经在跑。

        `force=True` 只给"已经自己认领过"的调用方（看门狗的 `resume_pending`）；
        普通建任务/重试一律走认领，避免多实例重复执行同一任务（第三轮 P0-2）。
        """
        running = self._tasks.get(task_id)
        if running is not None and not running.done():
            return task_id
        if not force:
            lease = int(getattr(self.settings, "task_lease_seconds", 300) or 300)
            with session_scope() as session:
                if not task_store.claim(session, task_id, lease_seconds=lease):
                    return task_id          # 已被认领/不在 queued：不重复派发
        self._tasks[task_id] = asyncio.create_task(self._run(task_id), name=f"image-task-{task_id}")
        return task_id

    async def _run(self, task_id: str) -> None:
        from app.agents_image.graph import execute_task

        async with self._semaphore:
            with session_scope() as session:
                task = task_store.get_task(session, task_id)
                if task is None or task.status in {"canceled", "done", "failed"}:
                    return
                task_trace = str(getattr(task, "trace_id", "") or "")
                task_store.mark_running(session, task_id)
                # 拿到并发槽位后再续一次租约：排队时间再长也不会被别的实例/看门狗抢走
                task_store.renew_lease(
                    session, task_id,
                    seconds=int(getattr(self.settings, "task_lease_seconds", 300) or 300))
            self._in_flight += 1
            # P1-1（第十轮）：后台执行体必须绑定任务的 trace——
            # 否则执行期的日志（graph/settlement/provider）全部是 [trace=-]，
            # 与创建请求的 trace 串不起来（task_id 只能串同一条任务，串不了请求）。
            from app.observability import trace as trace_mod

            try:
                if task_trace:
                    with trace_mod.trace_context(task_trace, agent_id="image"):
                        await self._execute(task_id, execute_task)
                else:
                    await self._execute(task_id, execute_task)
            finally:
                self._in_flight -= 1
                self._tasks.pop(task_id, None)

    async def _execute(self, task_id: str, execute_task) -> None:  # noqa: ANN001 —— 可注入以便测试
        try:
            await execute_task(task_id)
        except Exception as exc:  # noqa: BLE001 —— 任务失败必须落库，不能只打日志
            logger.warning("生图任务执行失败 %s：%s", task_id, exc)
            from app.agents_image import settlement
            from app.agents_image.generator import handler as generator_handler
            from app.core.errors import normalize

            err = normalize(exc)
            # P1-6/P1-2：失败路径要记**真实发生的事**——发起量 + 已生成张数 + 成本。
            # provider 跑到第 N 张失败时，前 N-1 张已经生成、已经计费；过去只带发起量，
            # 结算就记成 attempted=0、成本 0（使用量/成本口径系统性低估）。
            stats = generator_handler.failure_stats_of(err)
            calls = stats["provider_calls"]
            generated = stats["generated"]
            spent = stats["cost_cny"]
            llm_spent = stats["llm_cost_cny"]
            with session_scope() as session:
                task_store.mark_failed(session, task_id, code=err.code, detail=err.detail)
                if calls or generated or spent or llm_spent:
                    task_store.update_task(session, task_id, provider_calls=calls,
                                           attempted_images=generated,
                                           external_cost_cny=spent,
                                           llm_cost_cny=llm_spent)
                # 释放预留 + 记失败使用量（图已经生成过，钱已经花掉）；
                # 若 graph 内部已经结算过（如全被门禁拦下），这里会被 settled_at 拦掉
                settlement.settle_cancel(session, task_id, attempted=generated,
                                         provider_calls=calls, cost=spent,
                                         llm_cost=llm_spent)

    async def cancel(self, task_id: str) -> bool:
        """取消任务：未调模型 → 释放预留额度并记一次失败；已结束的任务取消返回 False。"""
        from app.agents_image import settlement

        with session_scope() as session:
            task = task_store.get_task(session, task_id)
            if task is None or task.status in {"done", "failed"}:
                return False
            already_canceled = task.status == "canceled"
            if not already_canceled:
                task_store.update_task(session, task_id, status="canceled",
                                       finished_at=dt.datetime.now(),
                                       error_code="IMG_TASK_CANCELED",
                                       error_detail="用户在调用生图模型前取消")
            # 关键：额度必须在这里退回（过去只改状态，预留永远占着 → P0-1）
            settlement.settle_cancel(session, task_id, code="IMG_TASK_CANCELED",
                                     detail="用户在调用生图模型前取消",
                                     mark_canceled=False)
        running = self._tasks.pop(task_id, None)
        if running is not None and not running.done():
            running.cancel()
        return True

    async def resume_pending(self) -> list[str]:
        """续跑（启动时 + 看门狗周期调用）：只拾取**未被认领的 queued** 或 **租约过期的 running**。

        重投次数封顶：`recoveries > IMAGE_MAX_RECOVERIES` 时直接判失败并释放预留额度，
        避免"坏任务被无限重投、每次都产生真实成本"。第三轮 P0-2：只有**认领成功**的重投
        才 +recoveries——排队中但已被认领的任务不再被反复计数（过去 90–120 秒就被误杀）。
        """
        lease = int(getattr(self.settings, "task_lease_seconds", 300) or 300)
        grace = int(getattr(self.settings, "claim_grace_seconds", 30) or 0)
        max_recoveries = int(getattr(self.settings, "max_recoveries", 3) or 3)
        with session_scope() as session:
            candidates = task_store.list_recoverable(
                session, lease_seconds=lease, grace_seconds=grace)
        resumed: list[str] = []
        for candidate in candidates:
            with session_scope() as session:
                if candidate.kind == "queued":
                    claimed = task_store.claim(session, candidate.task_id, lease_seconds=lease)
                else:
                    claimed = task_store.claim_running(session, candidate.task_id,
                                                       lease_seconds=lease)
                if not claimed:
                    continue                 # 别人抢先接管/任务已不在候选状态
                recoveries = task_store.bump_recoveries(session, candidate.task_id)
                if recoveries > max_recoveries:
                    task_store.mark_failed(
                        session, candidate.task_id, code="IMG_RECOVERY_EXHAUSTED",
                        detail=f"看门狗已重投 {recoveries} 次仍未完成，判失败（不再重投）")
                    from app.agents_image import settlement

                    settlement.settle_cancel(session, candidate.task_id)
                    continue
            await self.submit(candidate.task_id, force=True)
            resumed.append(candidate.task_id)
        if resumed:
            logger.info("生图任务续跑：%d 个（%s）", len(resumed), resumed[:5])
        return resumed

    async def health(self) -> dict:
        return {"ok": True, "backend": self.name, "in_flight": self.in_flight,
                "concurrency": self.settings.concurrency}

    def reset(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._in_flight = 0
