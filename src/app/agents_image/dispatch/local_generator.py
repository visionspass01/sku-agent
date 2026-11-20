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

        P1-3（第十二轮）：认领是同步 DB，丢线程池——建任务派发循环里每个任务一次，
        满批 60 个任务就是 60 次阻塞往返压在事件循环上。
        """
        from app.observability.trace import run_in_trace

        running = self._tasks.get(task_id)
        if running is not None and not running.done():
            return task_id
        if not force:
            lease = int(getattr(self.settings, "task_lease_seconds", 300) or 300)

            def _claim() -> bool:
                with session_scope() as session:
                    return task_store.claim(session, task_id, lease_seconds=lease)

            if not await run_in_trace(_claim):
                return task_id          # 已被认领/不在 queued：不重复派发
        self._tasks[task_id] = asyncio.create_task(self._run(task_id), name=f"image-task-{task_id}")
        return task_id

    async def _run(self, task_id: str) -> None:
        from app.agents_image.graph import execute_task
        from app.observability.trace import run_in_trace

        async with self._semaphore:
            def _start() -> str | None:
                """同步起点：状态判定 + 认领执行权 + 续约（P1-3：整块丢线程池）。"""
                with session_scope() as session:
                    task = task_store.get_task(session, task_id)
                    if task is None or task.status in {"canceled", "done", "failed"}:
                        return None
                    task_trace = str(getattr(task, "trace_id", "") or "")
                    # P1-5（第十二轮）：用 CAS 认领执行权（只允许 queued → running）。
                    # 上一版是"先读状态、再无条件写 running"，两步之间用户取消会被覆盖回
                    # running：界面显示已取消，任务却继续出图并按全价结算。
                    if not task_store.mark_running(session, task_id):
                        return None
                    # 拿到并发槽位后再续一次租约：排队时间再长也不会被别的实例/看门狗抢走
                    task_store.renew_lease(
                        session, task_id,
                        seconds=int(getattr(self.settings, "task_lease_seconds", 300) or 300))
                    return task_trace

            task_trace = await run_in_trace(_start)
            if task_trace is None:
                return
            self._in_flight += 1
            # P2-2（第十二轮）：`metrics.set_in_flight` 过去没有任何调用点，
            # Prometheus 的 `image_in_flight` 与 health 段的在途数永远是 0（死指标）。
            from app.agents_image import metrics as image_metrics

            image_metrics.set_in_flight(self._in_flight)
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
                image_metrics.set_in_flight(self._in_flight)
                self._tasks.pop(task_id, None)

    async def _execute(self, task_id: str, execute_task) -> None:  # noqa: ANN001 —— 可注入以便测试
        from app.observability.trace import run_in_trace

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

            def _record_failure() -> None:
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

            # P1-3（第十二轮）：失败落库也是同步 DB，丢线程池（失败路径不能反过来卡住 loop）
            await run_in_trace(_record_failure)

    async def cancel(self, task_id: str) -> bool:
        """取消任务：未调模型 → 释放预留额度并记一次失败；已结束的任务取消返回 False。"""
        from app.agents_image import settlement
        from app.observability.trace import run_in_trace

        def _cancel_db() -> bool:
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
                return True

        # P1-3（第十二轮）：取消判定/结算都是同步 DB，丢线程池
        if not await run_in_trace(_cancel_db):
            return False
        running = self._tasks.pop(task_id, None)
        if running is not None and not running.done():
            running.cancel()
        return True

    async def resume_pending(self) -> list[str]:
        """续跑（启动时 + 看门狗周期调用）：只拾取**未被认领的 queued** 或 **租约过期的 running**。

        重投次数封顶：`recoveries > IMAGE_MAX_RECOVERIES` 时直接判失败并释放预留额度，
        避免"坏任务被无限重投、每次都产生真实成本"。第三轮 P0-2：只有**认领成功**的重投
        才 +recoveries——排队中但已被认领的任务不再被反复计数（过去 90–120 秒就被误杀）。

        P1-1（第十一轮）：接管一个"租约过期的 running"任务前，先把任务台账里**已经发生的**
        发起量/张数/成本结算进使用量流水，再把台账字段清零给新一次执行用。
        否则崩溃/被抢的任务重投会重复花钱，而账一直是 0（第二次结算只记本轮）。

        P1-3（第十二轮）：候选扫描与逐条认领都是同步 DB，整块丢线程池
        （看门狗在服务进程内跑，压在 loop 上就是周期性的全站抖动）。
        """
        from app.observability.trace import run_in_trace

        lease = int(getattr(self.settings, "task_lease_seconds", 300) or 300)
        grace = int(getattr(self.settings, "claim_grace_seconds", 30) or 0)
        max_recoveries = int(getattr(self.settings, "max_recoveries", 3) or 3)

        def _list_candidates():  # noqa: ANN202 —— 内部辅助
            with session_scope() as session:
                return task_store.list_recoverable(
                    session, lease_seconds=lease, grace_seconds=grace)

        candidates = await run_in_trace(_list_candidates)
        resumed: list[str] = []
        for candidate in candidates:
            def _claim_candidate(cand=candidate) -> str:  # noqa: ANN001 —— 内部辅助
                """返回 skip / exhausted / claimed 三种结论。"""
                with session_scope() as session:
                    if cand.kind == "queued":
                        claimed = task_store.claim(session, cand.task_id, lease_seconds=lease)
                    else:
                        claimed = task_store.claim_running(session, cand.task_id,
                                                           lease_seconds=lease)
                    if not claimed:
                        return "skip"          # 别人抢先接管/任务已不在候选状态
                    if cand.kind != "queued":
                        _flush_partial_cost(session, cand.task_id)
                    recoveries = task_store.bump_recoveries(session, cand.task_id)
                    if recoveries > max_recoveries:
                        task_store.mark_failed(
                            session, cand.task_id, code="IMG_RECOVERY_EXHAUSTED",
                            detail=f"看门狗已重投 {recoveries} 次仍未完成，判失败（不再重投）")
                        from app.agents_image import settlement

                        settlement.settle_cancel(session, cand.task_id)
                        return "exhausted"
                    return "claimed"

            outcome = await run_in_trace(_claim_candidate)
            if outcome != "claimed":
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


def _flush_partial_cost(session, task_id: str) -> None:  # noqa: ANN001 —— sqlalchemy Session
    """P1-1（第十一轮）：把任务台账里"已经发生"的发起量/张数/成本结算进使用量流水，
    并把台账字段清零（新一次执行从 0 开始，避免重复记账）。

    触发场景：进程崩溃/被 kill 后被看门狗接管；或单个 provider 调用超时超过租约被抢。
    过去这些已花的钱没有任何数据源，重投又再花一遍，账却一直是 0。
    """
    from app.agents_image.store import usage as usage_store

    task = task_store.get_task(session, task_id)
    if task is None:
        return
    attempted = int(getattr(task, "attempted_images", 0) or 0)
    calls = int(getattr(task, "provider_calls", 0) or 0)
    cost = float(getattr(task, "external_cost_cny", 0.0) or 0.0)
    llm_cost = float(getattr(task, "llm_cost_cny", 0.0) or 0.0)
    if not (attempted or calls or cost or llm_cost):
        return
    usage_store.record(session, username=task.username, department=task.department,
                       attempted=attempted, delivered=attempted, provider_calls=calls,
                       cost=cost, llm_cost=llm_cost)
    task_store.update_task(session, task_id, attempted_images=0, external_cost_cny=0.0,
                           llm_cost_cny=0.0, provider_calls=0)
    logger.warning("接管已中断任务：先把已发生成本入账（task=%s 张数=%s 成本=%s 发起=%s）",
                   task_id, attempted, round(cost, 4), calls)
