"""结算唯一入口（第三轮修复）：交付结算 / 取消结算 / 重试重置。

为什么单独一个模块：
- 额度（`image_quota.reserved`）是"钱"，释放/转正**只能发生一次**——取消、失败、交付三条
  路径过去各写各的，结果取消"未调模型"的任务时预留永远不释放（P0-1）；
- 使用量流水（`image_usage`）与成本口径也必须与结算同源，否则取消/失败的成本会被漏记；
- `settled_at` 做**结算权**（条件 UPDATE），并发下只有一条路径能记账。

口径（与功能8 一致）：
- 使用量 = 第三方成功生成张数（`attempted`，含被门禁拦下的）；
- 交付张数 = Agent 审核通过（`delivered`）；
- 配额按**使用量**占额度，重试轮次按本轮计划张数封顶；
- 取消/失败：额度全退，但使用量/成本照记（provider 已经跑过、钱已经花掉）。
"""

from __future__ import annotations

import datetime as dt
import logging

from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import usage as usage_store
from app.agents_image.store import users as user_store

logger = logging.getLogger("sku-agent.image.settlement")


def _task_of(session, task_id: str):
    task = task_store.get_task(session, task_id)
    if task is None:
        return None
    return task


def _is_admin(session, username: str | None) -> bool:
    try:
        row = user_store.get_user(session, username or "")
        return bool(row is not None and (row.role or "") == "admin")
    except Exception:  # noqa: BLE001 —— 账号查不到按普通用户处理
        return False


def _write_warnings(session, task_id: str, warnings: list[str] | None) -> None:
    """把本轮告警写回任务（**合并**而不是覆盖，见 `task_store.merge_warnings`）。

    建任务阶段也会写告警（例如 iterate 命中不了复用的 `IMG_REUSE_SKIPPED`），
    结算时如果直接覆盖，这些"为什么没复用/为什么少图"的说明会在任务完成那一刻凭空消失。
    """
    if warnings is None:
        return
    task_store.merge_warnings(session, task_id, warnings)


def settle_done(session, task_id: str, *, delivered: int, attempted: int, cost: float,
                provider_calls: int = 0, warnings: list[str] | None = None) -> dict:
    """交付结算：把使用量占成 used，多预留的释放；记使用量流水；写告警。

    返回 `{"settled": bool, "charged": int}`：`settled=False` 表示这笔任务已经被别的路径
    （取消/失败）结算过，调用方应停止记账（幂等）。
    """
    task = _task_of(session, task_id)
    if task is None:
        return {"settled": False, "charged": 0}
    if not task_store.mark_settled(session, task_id):
        logger.warning("重复结算被拦截（已结算过）：task=%s", task_id)
        return {"settled": False, "charged": 0}
    planned = int(getattr(task, "image_count", 0) or getattr(task, "n", 0) or 0)
    charged = min(max(int(attempted), int(delivered)), planned or 0)
    username = task.username
    if planned and not _is_admin(session, username):
        if not quota_store.commit_reserve(session, username, charged):
            mismatch = (f"IMG_QUOTA_SETTLE_MISMATCH: 预留额度与结算张数不一致"
                        f"（计划 {planned} 张 / 结算 {charged} 张），已按剩余预留释放")
            if warnings is not None:
                warnings.append(mismatch)
            logger.warning("配额结算对账不一致：task=%s planned=%s charged=%s",
                           task_id, planned, charged)
        quota_store.release(session, username, max(0, planned - charged))
    usage_store.record(session, username=username, department=task.department,
                       delivered=attempted, attempted=attempted, tasks=1, cost=cost,
                       provider_calls=provider_calls, agent_passed=delivered)
    _write_warnings(session, task_id, warnings)
    return {"settled": True, "charged": charged}


def settle_cancel(session, task_id: str, *, attempted: int = 0, provider_calls: int = 0,
                  cost: float = 0.0, warnings: list[str] | None = None,
                  code: str = "", detail: str = "", mark_canceled: bool = False) -> bool:
    """取消/失败结算：额度全退，使用量/成本照记（钱已经花掉）。

    `mark_canceled=True` 时顺带把任务标成 `canceled`（用于"未调模型就取消"的路径）。
    `code/detail` 只在 `mark_canceled=True` 时写。
    """
    task = _task_of(session, task_id)
    if task is None:
        return False
    if not task_store.mark_settled(session, task_id):
        logger.info("取消结算被跳过（已结算过）：task=%s", task_id)
        if mark_canceled and task.status not in {"done", "failed", "canceled"}:
            task_store.update_task(session, task_id, status="canceled",
                                   finished_at=dt.datetime.now(),
                                   error_code=code or "IMG_TASK_CANCELED",
                                   error_detail=detail)
        return False
    planned = int(getattr(task, "image_count", 0) or getattr(task, "n", 0) or 0)
    if planned and not _is_admin(session, task.username):
        quota_store.release(session, task.username, planned)
    usage_store.record(session, username=task.username, department=task.department,
                       failed=1, tasks=1, attempted=attempted, delivered=attempted,
                       provider_calls=provider_calls, cost=cost)
    if mark_canceled:
        task_store.update_task(session, task_id, status="canceled",
                               finished_at=dt.datetime.now(),
                               error_code=code or "IMG_TASK_CANCELED",
                               error_detail=detail,
                               cancel_requested=False,
                               attempted_images=int(attempted or 0))
    _write_warnings(session, task_id, warnings)
    return True


def reset_for_retry(session, task_id: str) -> bool:
    """重试前把任务重置成"可再次执行"的干净状态（P1-10 / P2-11）。

    - `recoveries=0`：否则重试后第一次看门狗扫描就会按"重投超限"判失败（P1-10）；
    - `settled_at=None`：额度会重新预留，重试后的结算必须能再次记账；
    - 清掉上一轮的 warnings/blocked/reused/listing，避免残留状态骗人。
    """
    task = _task_of(session, task_id)
    if task is None:
        return False
    task_store.update_task(
        session, task_id, status="queued",
        error_code="", error_detail="", finished_at=None,
        attempts=0, attempted_images=0, delivered_images=0,
        provider_started_at=None, cancel_requested=False, lease_until=None,
        recoveries=0, settled_at=None, provider_calls=0,
        warnings_json=[], blocked_json=[], reused_images=0,
        listing_status="not_listed", listed_at=None, listing_url="",
    )
    return True


__all__ = ["settle_done", "settle_cancel", "reset_for_retry"]
