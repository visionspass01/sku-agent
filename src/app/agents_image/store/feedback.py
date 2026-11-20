"""人工采纳/驳回（M6）：采纳 → 正样本，驳回 → 负样本（含原因），成本不返还。

为什么一定要有人工闭环：Critic 只能判"结构/规则"，判不了"这张能不能上架"。
真实工作流是运营从 N 张里挑 1 张、其余驳回；这些判断才是最有价值的训练/评测信号。
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agents_image.store.models import ImageAsset, ImageFeedback, ImageMemory
from app.core.errors import raise_image

logger = logging.getLogger("sku-agent.image.feedback")

VERDICTS = {"adopted": "adopted", "rejected": "rejected"}
REUSE_SOURCE = "reuse"


def _usage_field(verdict: str, *, reused: bool) -> str:
    """判断结论落在使用量的哪个字段（第四轮 P1-1）。

    - 新生成图采纳 → `adopted`（上架率分子，分母是使用量=新生成张数）；
    - 复用图采纳 → `reused_adopted`（复用图不在使用量分母里，混进 `adopted` 会让上架率 >100%）；
    - 驳回一律 `rejected`（负样本计数，不进上架率分子）。
    """
    if verdict == "adopted":
        return "reused_adopted" if reused else "adopted"
    return "rejected"


def record(session, *, task_id: str, asset_id: str, verdict: str, reason: str = "",
           username: str = "", slot: int = 0) -> dict:
    """记录一次人工判断（同一张图只能有一种结论，重复提交按覆盖处理）。

    口径（第三/四轮）：
    - **按当前终态记账**：adopt→reject→adopt 不再每换一次就 +1（P1-2），
      而是先减掉旧态、再加上新态，字段下限 0；
    - 只有交付产出图（`kind=output`）能采纳/驳回：被像素门禁拦下的图（`kind=blocked`）
      必须走 unblock 人工放行后才算交付（P2-13），否则"被拦的图直接上架"；
    - 复用图采纳单独记 `reused_adopted`，不计入上架率（P1-1）。
    """
    verdict = (verdict or "").strip().lower()
    if verdict not in VERDICTS:
        raise_image("IMG_INPUT_INVALID", f"不支持的判断：{verdict}（adopted / rejected）")
    asset = session.get(ImageAsset, asset_id)
    if asset is None:
        raise_image("IMG_TASK_NOT_FOUND", f"图片不存在：{asset_id}")
    kind = (asset.kind or "").strip()
    if kind != "output":
        raise_image(
            "IMG_INPUT_INVALID",
            f"只有交付产出图能采纳/驳回（当前 kind={kind or '未知'}）；"
            "被门禁拦下的图请先调用 unblock 人工放行")
    row = session.execute(
        select(ImageFeedback).where(ImageFeedback.asset_id == asset_id)
    ).scalars().first()
    previous = (row.verdict if row is not None else "") or ""
    first_created_at = row.created_at if row is not None else None
    if row is None:
        new_row = ImageFeedback(task_id=task_id, asset_id=asset_id, slot=slot or asset.slot,
                                verdict=verdict, reason=reason[:500], username=username,
                                created_at=dt.datetime.now())
        try:
            with session.begin_nested():      # SAVEPOINT：并发双击只回滚这一条 INSERT
                session.add(new_row)
                session.flush()
            row = new_row
        except IntegrityError:
            # P2-9（第九轮）：并发双击同一张图（前端连点/双开）时，唯一索引 `uq_image_feedback_asset`
            # 会让后到者撞键；上一版直接 500。现在退回"覆盖更新"，并以**胜者写入的状态**为准
            # 重新计算 changed：两个并发 adopt 只会记一次采纳，不会重复加计。
            row = session.execute(
                select(ImageFeedback).where(ImageFeedback.asset_id == asset_id)
            ).scalars().first()
            if row is None:                   # pragma: no cover —— 撞键后却读不到行
                raise
            previous = str(row.verdict or "")
            row.verdict = verdict
            row.reason = reason[:500]
            row.username = username or row.username
    else:
        row.verdict = verdict
        row.reason = reason[:500]
        row.username = username or row.username
    asset.adopt_status = verdict
    asset.adopt_reason = reason[:500]
    session.flush()

    # 采纳/驳回更新使用量：成本早已按 provider 成功计入，不返还；重复提交不重复计数
    changed = previous != verdict
    if changed:
        reused = (asset.source or "") == REUSE_SOURCE
        # P2-5（第八轮）：使用量要记在**图片归属人**名下——管理员代运营采纳时，
        # 记成操作人会让原主人的上架率一直是 0，而管理员的 adopted 虚增。
        target_user = asset.username or username
        try:
            from app.agents_image.store import usage as usage_store

            # 回退旧态写回它当时落账的那个小时桶；新态记在当前小时桶
            if previous:
                rollback = {_usage_field(previous, reused=reused): -1}
                usage_store.shift(session, username=target_user, department=asset.department,
                                  now=first_created_at, **rollback)
            usage_store.shift(session, username=target_user, department=asset.department,
                              **{_usage_field(verdict, reused=reused): 1})
        except Exception as exc:  # noqa: BLE001 —— 统计失败不影响判断落库
            from app.agents_image import metrics as image_metrics

            image_metrics.record_swallowed("feedback_usage")
            logger.warning("人工判断的使用量记账失败：asset=%s %s", asset_id, exc)

    # 正/负样本进 image_memory（Reflexion 优先用负样本规避同类问题）
    try:
        signature = f"{verdict}|{asset.product_id or ''}|slot{row.slot}"
        memory = session.execute(
            select(ImageMemory).where(ImageMemory.failure_signature == signature)
        ).scalars().first()
        if memory is None:
            session.add(ImageMemory(
                memory_id=f"mem-{verdict}-{asset_id[:12]}", department=asset.department,
                failure_signature=signature,
                fix_summary=(f"采纳原因：{reason}" if verdict == "adopted" else f"驳回原因：{reason}"),
                prompt_patch=(reason if verdict == "rejected" else ""),
                hits=1, sample_kind=("positive" if verdict == "adopted" else "negative"),
                adopt_count=1 if verdict == "adopted" else 0,
                created_at=dt.datetime.now(),
            ))
        else:
            memory.hits = int(memory.hits or 0) + 1
            if verdict == "adopted":
                memory.adopt_count = int(memory.adopt_count or 0) + 1
            if reason:
                memory.fix_summary = reason[:500]
        session.flush()
    except Exception as exc:  # noqa: BLE001 —— 样本归档失败不影响判断
        from app.agents_image import metrics as image_metrics

        image_metrics.record_swallowed("feedback_memory")
        logger.warning("样本归档失败：asset=%s %s", asset_id, exc)

    return {"asset_id": asset_id, "task_id": task_id, "verdict": verdict,
            "reason": row.reason, "slot": row.slot, "username": row.username}


def summary(session, *, task_id: str | None = None) -> dict:
    """采纳/驳回统计（可按任务过滤）。"""
    statement = select(ImageFeedback)
    if task_id:
        statement = statement.where(ImageFeedback.task_id == task_id)
    rows = session.execute(statement).scalars().all()
    adopted = sum(1 for row in rows if row.verdict == "adopted")
    rejected = sum(1 for row in rows if row.verdict == "rejected")
    return {"total": len(rows), "adopted": adopted, "rejected": rejected,
            "adoption_rate": round(adopted / len(rows), 4) if rows else 0.0}


def list_feedback(session, *, task_id: str | None = None, limit: int = 50) -> list[dict]:
    statement = select(ImageFeedback).order_by(ImageFeedback.created_at.desc()).limit(limit)
    if task_id:
        statement = statement.where(ImageFeedback.task_id == task_id)
    rows = session.execute(statement).scalars().all()
    return [{"asset_id": row.asset_id, "task_id": row.task_id, "slot": row.slot,
             "verdict": row.verdict, "reason": row.reason, "username": row.username,
             "created_at": row.created_at.isoformat() if row.created_at else ""}
            for row in rows]


def samples(session, *, kind: str | None = None, limit: int = 50) -> list[dict]:
    """正/负样本清单（`kind=positive|negative|auto`）。"""
    statement = select(ImageMemory).order_by(ImageMemory.created_at.desc()).limit(limit)
    if kind:
        statement = statement.where(ImageMemory.sample_kind == kind)
    rows = session.execute(statement).scalars().all()
    return [{"memory_id": row.memory_id, "sample_kind": row.sample_kind,
             "signature": row.failure_signature, "fix_summary": row.fix_summary,
             "prompt_patch": row.prompt_patch, "hits": int(row.hits or 0),
             "adopt_count": int(row.adopt_count or 0)} for row in rows]


__all__ = ["VERDICTS", "record", "summary", "list_feedback", "samples"]
