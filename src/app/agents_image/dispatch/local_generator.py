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
        """启动时续跑：queued 或超时 running 的任务重新提交。"""
        timeout = self.settings.timeout_seconds
        with session_scope() as session:
            rows = task_store.list_recoverable(session, timeout_seconds=timeout)
            ids = [row.task_id for row in rows]
        for task_id in ids:
            await self.submit(task_id)
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
