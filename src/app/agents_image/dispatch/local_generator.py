"""本地调度器：进程内 asyncio 任务 + 信号量（开发/测试默认），状态全在 DB。

- `submit(task_id)`：起一个 asyncio 任务去跑 `graph.execute_task`；
- `resume_pending()`：启动/重启后把 `queued` 或超时 `running` 的任务重新提交（幂等）；
- 取消：DB 标记 `canceled`，执行体在关键点检查。
"""

from __future__ import annotations

import asyncio
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

    async def submit(self, task_id: str) -> str:
        running = self._tasks.get(task_id)
        if running is not None and not running.done():
            return task_id
        self._tasks[task_id] = asyncio.create_task(self._run(task_id), name=f"image-task-{task_id}")
        return task_id

    async def _run(self, task_id: str) -> None:
        from app.agents_image.graph import execute_task

        async with self._semaphore:
            with session_scope() as session:
                task = task_store.get_task(session, task_id)
                if task is None or task.status in {"canceled", "done", "failed"}:
                    return
                task_store.mark_running(session, task_id)
            self._in_flight += 1
            try:
                await execute_task(task_id)
            except Exception as exc:  # noqa: BLE001 —— 任务失败必须落库，不能只打日志
                logger.warning("生图任务执行失败 %s：%s", task_id, exc)
                from app.core.errors import normalize

                err = normalize(exc)
                with session_scope() as session:
                    task_store.mark_failed(session, task_id, code=err.code, detail=err.detail)
                    # 失败要释放创建任务时预留的额度（否则额度会被永久占住）
                    try:
                        from app.agents_image.store import quota as quota_store

                        task = task_store.get_task(session, task_id)
                        planned = int(getattr(task, "image_count", 0) or 0) if task else 0
                        if planned:
                            quota_store.release(session, task.username, planned)
                    except Exception:  # noqa: BLE001 —— 释放失败不影响失败落库
                        logger.warning("失败任务释放预留额度出错：%s", task_id)
            finally:
                self._in_flight -= 1
                self._tasks.pop(task_id, None)

    async def cancel(self, task_id: str) -> bool:
        with session_scope() as session:
            task = task_store.get_task(session, task_id)
            if task is None or task.status in {"done", "failed"}:
                return False
            task_store.update_task(session, task_id, status="canceled")
        running = self._tasks.pop(task_id, None)
        if running is not None and not running.done():
            running.cancel()
        return True

    async def resume_pending(self) -> list[str]:
        """续跑（启动时 + 看门狗周期调用）：只拾取 `queued` 或 **租约过期** 的任务。

        重投次数封顶：`recoveries > IMAGE_MAX_RECOVERIES` 时直接判失败并释放预留额度，
        避免"坏任务被无限重投、每次都产生真实成本"。
        """
        lease = int(getattr(self.settings, "task_lease_seconds", 300) or 300)
        max_recoveries = int(getattr(self.settings, "max_recoveries", 3) or 3)
        with session_scope() as session:
            rows = task_store.list_recoverable(
                session, timeout_seconds=self.settings.timeout_seconds,
                lease_seconds=lease, max_recoveries=max_recoveries)
            ids = [row.task_id for row in rows]
        resumed: list[str] = []
        for task_id in ids:
            with session_scope() as session:
                recoveries = task_store.bump_recoveries(session, task_id)
                if recoveries > max_recoveries:
                    task = task_store.get_task(session, task_id)
                    username = getattr(task, "username", "") if task else ""
                    planned = int(getattr(task, "image_count", 0) or 0) if task else 0
                    task_store.mark_failed(
                        session, task_id, code="IMG_RECOVERY_EXHAUSTED",
                        detail=f"看门狗已重投 {recoveries} 次仍未完成，判失败（不再重投）")
                    if username and planned:
                        from app.agents_image.store import quota as quota_store

                        try:
                            quota_store.release(session, username, planned)
                        except Exception:  # noqa: BLE001 —— 释放失败不影响判失败
                            logger.warning("重投超限任务释放预留失败：%s", task_id)
                    continue
            await self.submit(task_id)
            resumed.append(task_id)
        ids = resumed
        if ids:
            logger.info("生图任务续跑：%d 个（%s）", len(ids), ids[:5])
        return ids

    async def health(self) -> dict:
        return {"ok": True, "backend": self.name, "in_flight": self.in_flight,
                "concurrency": self.settings.concurrency}

    def reset(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._in_flight = 0
