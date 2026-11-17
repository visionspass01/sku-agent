"""`image_memory`：失败签名 → 修复经验的存取（Critic 不通过时归档，下次同因优先复用）。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.agents_image.store.models import ImageMemory
from app.core.errors import raise_image


def remember(session, *, signature: str, fix_summary: str, prompt_patch: str = "",
             department: str | None = None) -> ImageMemory | None:
    """同签名已存在就累加命中数，否则新建；`signature` 为空直接跳过（不产生垃圾记忆）。"""
    signature = (signature or "").strip()
    if not signature:
        return None
    row = session.execute(
        select(ImageMemory).where(ImageMemory.failure_signature == signature)
    ).scalars().first()
    if row is not None:
        row.hits = int(row.hits or 0) + 1
        if fix_summary and not row.fix_summary:
            row.fix_summary = fix_summary
        if prompt_patch:
            row.prompt_patch = prompt_patch
        session.flush()
        return row
    row = ImageMemory(memory_id=f"mem-{dt.datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                      department=department, failure_signature=signature,
                      fix_summary=fix_summary, prompt_patch=prompt_patch, hits=1,
                      created_at=dt.datetime.now())
    session.add(row)
    session.flush()
    return row


def lookup(session, signature: str) -> ImageMemory | None:
    return session.execute(
        select(ImageMemory).where(ImageMemory.failure_signature == (signature or ""))
    ).scalars().first()


def patches_for(session, signature: str) -> dict[str, str]:
    """把历史记忆翻成 `{issue_kind: patch}`，供 reflexion 优先使用。"""
    row = lookup(session, signature)
    if row is None or not row.prompt_patch:
        return {}
    kinds = [item for item in (row.failure_signature or "").split("|")[0].split("+") if item]
    return {kind: row.prompt_patch for kind in kinds}


def list_memories(session, *, limit: int = 20) -> list[dict]:
    rows = session.execute(
        select(ImageMemory).order_by(ImageMemory.hits.desc(), ImageMemory.created_at.desc())
        .limit(max(1, int(limit)))
    ).scalars().all()
    return [{"memory_id": row.memory_id, "failure_signature": row.failure_signature,
             "fix_summary": row.fix_summary, "prompt_patch": row.prompt_patch,
             "hits": int(row.hits or 0), "department": row.department}
            for row in rows]


def require_memory(session, memory_id: str) -> ImageMemory:
    row = session.get(ImageMemory, memory_id)
    if row is None:
        raise_image("IMG_INPUT_INVALID", f"修复记忆不存在：{memory_id}")
    return row


__all__ = ["remember", "lookup", "patches_for", "list_memories", "require_memory"]
