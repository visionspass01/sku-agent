"""人工采纳/驳回（M6）：采纳 → 正样本，驳回 → 负样本（含原因），成本不返还。

为什么一定要有人工闭环：Critic 只能判"结构/规则"，判不了"这张能不能上架"。
真实工作流是运营从 N 张里挑 1 张、其余驳回；这些判断才是最有价值的训练/评测信号。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.agents_image.store.models import ImageAsset, ImageFeedback, ImageMemory
from app.core.errors import raise_image

VERDICTS = {"adopted": "adopted", "rejected": "rejected"}


def _usage_kind(verdict: str) -> str:
    return "adopted" if verdict == "adopted" else "rejected"


def record(session, *, task_id: str, asset_id: str, verdict: str, reason: str = "",
           username: str = "", slot: int = 0) -> dict:
    """记录一次人工判断（同一张图只能有一种结论，重复提交按覆盖处理）。"""
    verdict = (verdict or "").strip().lower()
    if verdict not in VERDICTS:
        raise_image("IMG_INPUT_INVALID", f"不支持的判断：{verdict}（adopted / rejected）")
    asset = session.get(ImageAsset, asset_id)
    if asset is None:
        raise_image("IMG_TASK_NOT_FOUND", f"图片不存在：{asset_id}")
    row = session.execute(
        select(ImageFeedback).where(ImageFeedback.asset_id == asset_id)
    ).scalars().first()
    if row is None:
        row = ImageFeedback(task_id=task_id, asset_id=asset_id, slot=slot or asset.slot,
                            verdict=verdict, reason=reason[:500], username=username,
                            created_at=dt.datetime.now())
        session.add(row)
    else:
        row.verdict = verdict
        row.reason = reason[:500]
        row.username = username or row.username
    asset.adopt_status = verdict
    asset.adopt_reason = reason[:500]
    session.flush()

    # 采纳/驳回都会更新使用量（成本早已按 provider 成功计入，不返还）
    try:
        from app.agents_image.store import usage as usage_store

        with_usage = usage_store.record(
            session, username=username or asset.username, department=asset.department,
            **{_usage_kind(verdict): 1})
        _ = with_usage
    except Exception:  # noqa: BLE001 —— 统计失败不影响判断落库
        pass

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
    except Exception:  # noqa: BLE001 —— 样本归档失败不影响判断
        pass

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
