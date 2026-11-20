"""幂等表 `image_request` 的清理（P2-4，第九轮）。

这张表为「同 key 重放首次响应」保存完整响应体（含整批任务列表），只增不删会无限增长；
`runtime.py` 的清理循环本来只管文件/令牌/outbox/修复记忆，没管它。
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import delete, or_

from app.agents_image.store.models import ImageRequest

logger = logging.getLogger("sku-agent.image.requests")

DEFAULT_KEEP_HOURS = 72
DEFAULT_PENDING_MINUTES = 60


def prune(*, keep_hours: int | None = None, pending_minutes: int | None = None,
          now: dt.datetime | None = None) -> int:
    """删除过期幂等记录：`done` 超保留期、`pending` 超滞留期（孤立的半成品请求）。

    返回删除条数。失败由调用方吞掉（清理失败不影响服务）。
    """
    from app.agents_image.config import get_image_settings

    settings = get_image_settings()
    keep = int(keep_hours if keep_hours is not None
               else getattr(settings, "request_keep_hours", DEFAULT_KEEP_HOURS))
    pending = int(pending_minutes if pending_minutes is not None
                  else getattr(settings, "request_pending_minutes", DEFAULT_PENDING_MINUTES))
    moment = now or dt.datetime.now()
    done_cutoff = moment - dt.timedelta(hours=max(0, keep))
    pending_cutoff = moment - dt.timedelta(minutes=max(0, pending))

    from app.db.engine import session_scope

    with session_scope() as session:
        result = session.execute(
            delete(ImageRequest)
            .where(or_(ImageRequest.status != "pending",
                       ImageRequest.status.is_(None)))
            .where(ImageRequest.created_at < done_cutoff)
        )
        removed = int(result.rowcount or 0)
        stale = session.execute(
            delete(ImageRequest)
            .where(ImageRequest.status == "pending")
            .where(ImageRequest.created_at < pending_cutoff)
        )
        removed += int(stale.rowcount or 0)
    if removed:
        logger.info("幂等表清理：删除 %s 条（done 保留 %s 小时 / pending 保留 %s 分钟）",
                    removed, keep, pending)
    return removed


__all__ = ["prune", "DEFAULT_KEEP_HOURS", "DEFAULT_PENDING_MINUTES"]
