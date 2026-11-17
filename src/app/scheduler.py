"""定时任务：每小时快照+筛查、每日全量巡检推送（APScheduler）。

**Demo 默认关闭**（`SCHEDULER_ENABLED=false`）：定时巡检会真实调用 LLM
（每小时 5 个 SKU + 每日 09:00 共 50 个 SKU），无人访问时也会持续产生 token 消耗。
需要展示定时能力时：`SCHEDULER_ENABLED=true` 重启即可；只想演示一次可调用
`POST /api/scheduler/run-once?job=daily`（管理员），走同一套 job 逻辑但不留定时器。
手动全量巡检（`POST /api/patrol`）不受本开关影响。
"""

from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.db import repos
from app.db.engine import session_scope
from app.pipeline.patrol import run_patrol
from app.pipeline.screening import screen
from app.pipeline.snapshot import aggregate_hour

logger = logging.getLogger("sku-agent.scheduler")
_scheduler: AsyncIOScheduler | None = None


async def hourly_job(limit: int | None = None, operator: str = "scheduler") -> dict:
    """每小时：聚合上一小时快照 → 多维度筛查 → 异常入队 → 小批量巡检。"""
    settings = get_settings()
    limit = limit or settings.scheduler_hourly_limit
    hour = dt.datetime.now().replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
    with session_scope() as session:
        aggregate_hour(session, hour)
        results = screen(session)
    logger.info("小时巡检完成：快照小时=%s 异常 SKU=%d", hour, len(results))
    summary = await run_patrol(mode="hourly", limit=limit, operator=operator)
    with session_scope() as session:
        repos.finish_run(
            session, summary["run_id"], status="ok",
            stats={**summary, "trigger": "scheduler-hourly"},
        )
    return summary


async def daily_job(limit: int | None = None, operator: str = "scheduler") -> dict:
    """每日 09:00：全量巡检 + 推送摘要。"""
    settings = get_settings()
    limit = limit or settings.scheduler_daily_limit
    summary = await run_patrol(mode="daily", limit=limit, operator=operator)
    with session_scope() as session:
        repos.finish_run(
            session, summary["run_id"], status="ok",
            stats={**summary, "trigger": "scheduler-daily"},
        )
        repos.add_notification(
            session, summary["run_id"],
            title="【SKU 监管】每日巡检报告",
            body=(
                f"巡检 {summary['processed']} 个异常 SKU：正常 {summary['ok']}，降级 {summary['degraded']}，"
                f"失败 {summary['failed']}；耗时 {summary['elapsed_ms'] / 1000:.1f}s。"
            ),
        )
    logger.info("每日巡检完成：%s", summary)
    return summary


JOBS = {
    "hourly": {"id": "hourly_screening", "cron": "每小时第 5 分钟", "func": hourly_job},
    "daily": {"id": "daily_patrol", "cron": "每日 09:00", "func": daily_job},
}


def start_scheduler() -> AsyncIOScheduler | None:
    global _scheduler
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info(
            "定时任务未启用（SCHEDULER_ENABLED=false，Demo 默认）："
            "手动全量巡检照常可用，可用 POST /api/scheduler/run-once 单次演练"
        )
        return None
    _scheduler = AsyncIOScheduler(timezone=settings.timezone)
    _scheduler.add_job(
        hourly_job, CronTrigger(minute=5), id=JOBS["hourly"]["id"], replace_existing=True,
    )
    _scheduler.add_job(
        daily_job, CronTrigger(hour=9, minute=0), id=JOBS["daily"]["id"], replace_existing=True,
    )
    _scheduler.start()
    logger.info("定时任务已启动：每小时筛查 + 每日 09:00 全量巡检")
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def scheduler_status() -> dict:
    """定时任务状态（供 /api/health 与 /api/scheduler/status 展示）。"""
    settings = get_settings()
    jobs: list[dict] = []
    if _scheduler is not None and getattr(_scheduler, "running", False):
        for job in _scheduler.get_jobs():
            next_run = getattr(job, "next_run_time", None)
            jobs.append({
                "id": job.id,
                "trigger": JOBS.get("hourly" if job.id == JOBS["hourly"]["id"] else "daily", {})
                .get("cron", str(getattr(job, "trigger", ""))),
                "next_run": next_run.isoformat(timespec="seconds") if next_run else None,
            })
    return {
        "enabled": bool(settings.scheduler_enabled),
        "running": bool(jobs),
        "timezone": settings.timezone,
        "jobs": jobs,
        "limits": {"hourly": settings.scheduler_hourly_limit,
                   "daily": settings.scheduler_daily_limit},
        "note": (
            "定时任务运行中：会真实调用 LLM 巡检"
            if jobs else
            "定时任务已关闭（Demo 默认）：手动全量巡检可用；单次演练用 POST /api/scheduler/run-once"
        ),
    }


def next_run_time() -> str | None:
    for job in scheduler_status().get("jobs") or []:
        if job.get("next_run"):
            return job["next_run"]
    return None
