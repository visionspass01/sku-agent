"""`image_memory`：修复经验的存取（第五轮 P0-4/P1-3/P1-4 重写）。

口径（为什么这么设计）：
- **失败不归档**：AI 生图能覆盖大多数情景，把每次失败当"经验"会让默认生成时的检索
  越来越吵、污染 Prompt，反而拉低成功率；
- **失败转成功才归档修法**：`remember_repair()` 存的是"这一轮真正生效的修复指令"
  （不是报错文本），按 `签名|部门` upsert，越用越准；
- **只在 Critic 不通过时才检索**：`repair_patches()` 由 graph 在准备再来一轮时调用；
- **只增不减**的问题用 `prune()` 解决：修复经验按 `updated_at` 做 TTL，人工采纳/驳回
  样本（`sample_kind=positive/negative`，`dedupe_key=NULL`）**永远不被自动清理**。
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import delete, select

from app.agents_image.store.models import ImageMemory
from app.core.errors import raise_image

logger = logging.getLogger("sku-agent.image.memory")

REPAIR_KIND = "repair"
KEEP_DAYS_DEFAULT = 90


def _repair_key(signature: str, department: str | None) -> str:
    return f"{(signature or '').strip()}|{(department or '').strip()}"[:160]


def _kinds_of(signature: str) -> list[str]:
    return [item for item in (signature or "").split("|")[0].split("+") if item]


def remember(session, *, signature: str, fix_summary: str, prompt_patch: str = "",
             department: str | None = None) -> ImageMemory | None:
    """人工样本/历史兼容入口：同签名已存在就累加命中数，否则新建（`sample_kind=auto`）。

    自动链路已不再调用它（失败不归档）；保留给人工反馈与历史数据读取。
    """
    signature = (signature or "").strip()
    if not signature:
        return None
    row = session.execute(
        select(ImageMemory).where(ImageMemory.failure_signature == signature,
                                  ImageMemory.dedupe_key.is_(None))
    ).scalars().first()
    if row is not None:
        row.hits = int(row.hits or 0) + 1
        if fix_summary and not row.fix_summary:
            row.fix_summary = fix_summary
        if prompt_patch:
            row.prompt_patch = prompt_patch
        row.updated_at = dt.datetime.now()
        session.flush()
        return row
    row = ImageMemory(memory_id=f"mem-{dt.datetime.now().strftime('%Y%m%d%H%M%S%f')}",
                      department=department, failure_signature=signature,
                      fix_summary=fix_summary, prompt_patch=prompt_patch, hits=1,
                      created_at=dt.datetime.now(), updated_at=dt.datetime.now())
    session.add(row)
    session.flush()
    return row


def remember_repair(session, *, signature: str, patch: str, department: str | None = None,
                    summary: str = "", now: dt.datetime | None = None) -> ImageMemory | None:
    """归档「失败→修复成功」的修法（P0-4）：按 `签名|部门` upsert，命中数 +1。"""
    signature = (signature or "").strip()
    patch = (patch or "").strip()
    if not signature or not patch:
        return None
    moment = now or dt.datetime.now()
    key = _repair_key(signature, department)
    row = session.execute(
        select(ImageMemory).where(ImageMemory.dedupe_key == key)
    ).scalars().first()
    if row is None:
        row = ImageMemory(
            memory_id=f"mem-repair-{key[:40]}", department=department,
            failure_signature=signature, fix_summary=(summary or "")[:500],
            prompt_patch=patch[:4000], hits=1, sample_kind=REPAIR_KIND,
            adopt_count=0, dedupe_key=key, created_at=moment, updated_at=moment,
        )
        session.add(row)
        session.flush()
        return row
    row.hits = int(row.hits or 0) + 1
    row.prompt_patch = patch[:4000]        # 最近一次生效的修法覆盖旧值
    if summary:
        row.fix_summary = summary[:500]
    row.updated_at = moment
    session.flush()
    return row


def repair_patches(session, signature: str, *, department: str | None = None,
                   now: dt.datetime | None = None) -> dict[str, str]:
    """检索同签名的历史修法（**同部门优先，其次通用**）；命中才累计 hits。

    返回 `{issue_kind: 修法}`，没有经验返回空 dict（调用方退回 KB fixes / 内置规则）。
    """
    signature = (signature or "").strip()
    if not signature:
        return {}
    rows = session.execute(
        select(ImageMemory).where(ImageMemory.failure_signature == signature,
                                  ImageMemory.dedupe_key.is_not(None))
    ).scalars().all()
    if not rows:
        return {}
    same = [row for row in rows if (row.department or "") == (department or "")]
    generic = [row for row in rows if not row.department]
    candidates = same or generic
    if not candidates:
        return {}
    row = max(candidates, key=lambda item: (int(item.hits or 0),
                                            item.updated_at or item.created_at or dt.datetime.min))
    row.hits = int(row.hits or 0) + 1
    row.updated_at = now or dt.datetime.now()
    session.flush()
    patch = str(row.prompt_patch or "").strip()
    if not patch:
        return {}
    return {kind: patch for kind in _kinds_of(signature)}


def prune(session, *, keep_days: int | None = None, now: dt.datetime | None = None) -> int:
    """清理过期修复经验（P1-4）：只清 `repair`/`auto`，人工样本一律保留。"""
    days = int(KEEP_DAYS_DEFAULT if keep_days is None else keep_days)
    cutoff = (now or dt.datetime.now()) - dt.timedelta(days=max(0, days))
    removed = 0
    for kind in (REPAIR_KIND, "auto"):
        result = session.execute(
            delete(ImageMemory)
            .where(ImageMemory.sample_kind == kind,
                   ImageMemory.updated_at.is_not(None),
                   ImageMemory.updated_at < cutoff)
        )
        removed += int(result.rowcount or 0)
        # 老数据没有 updated_at（本轮之前写入的 auto 行）：按 created_at 兜底
        legacy = session.execute(
            delete(ImageMemory)
            .where(ImageMemory.sample_kind == kind,
                   ImageMemory.updated_at.is_(None),
                   ImageMemory.created_at < cutoff)
        )
        removed += int(legacy.rowcount or 0)
    if removed:
        logger.info("修复经验清理：删除 %s 条（保留 %s 天）", removed, days)
    return removed


def lookup(session, signature: str) -> ImageMemory | None:
    return session.execute(
        select(ImageMemory).where(ImageMemory.failure_signature == (signature or ""))
    ).scalars().first()


def patches_for(session, signature: str) -> dict[str, str]:
    """兼容旧接口：优先返回修复经验（同签名），没有就退回旧 auto 行的 prompt_patch。"""
    repair = repair_patches(session, signature)
    if repair:
        return repair
    row = lookup(session, signature)
    if row is None or not row.prompt_patch:
        return {}
    return {kind: row.prompt_patch for kind in _kinds_of(signature)}


def list_memories(session, *, limit: int = 20, kind: str | None = None) -> list[dict]:
    statement = select(ImageMemory).order_by(ImageMemory.hits.desc(),
                                             ImageMemory.created_at.desc())
    if kind:
        statement = statement.where(ImageMemory.sample_kind == kind)
    rows = session.execute(statement.limit(max(1, int(limit)))).scalars().all()
    return [{"memory_id": row.memory_id, "failure_signature": row.failure_signature,
             "fix_summary": row.fix_summary, "prompt_patch": row.prompt_patch,
             "hits": int(row.hits or 0), "department": row.department,
             "sample_kind": row.sample_kind,
             "updated_at": row.updated_at.isoformat() if row.updated_at else ""}
            for row in rows]


def require_memory(session, memory_id: str) -> ImageMemory:
    row = session.get(ImageMemory, memory_id)
    if row is None:
        raise_image("IMG_INPUT_INVALID", f"修复记忆不存在：{memory_id}")
    return row


__all__ = ["REPAIR_KIND", "KEEP_DAYS_DEFAULT", "remember", "remember_repair",
           "repair_patches", "prune", "lookup", "patches_for", "list_memories",
           "require_memory"]
