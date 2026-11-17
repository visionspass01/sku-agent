"""图生图的最小编排：理解 → 规划图位 → 组装 Prompt → 校验 → 生图 → 落库。

D2–D3 的形态：不做 RAG / Critic / MCP / 配额（后续里程碑逐步接入），
但**节点边界与顺序**已按最终形态留好，后续只往里填内容。
"""

from __future__ import annotations

import logging

from app.agents_image import metrics
from app.agents_image import registry as reg_mod
from app.agents_image.config import get_image_settings
from app.agents_image.generator import handler as generator_handler
from app.agents_image.nodes import plan_slots, understand, validate
from app.agents_image.state import ImageTaskState, RefImage, SlotPlan
from app.agents_image.storage.image_storage import get_storage
from app.agents_image.store import assets as asset_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store.models import ImageAsset
from app.core.errors import ImageError
from app.db.engine import session_scope

logger = logging.getLogger("sku-agent.image.graph")


def _load_refs(session, ref_ids: list[str]) -> list[dict]:
    storage = get_storage()
    refs: list[dict] = []
    for asset_id in ref_ids:
        asset = asset_store.get_asset(session, asset_id)
        if asset is None:
            from app.core.errors import raise_image

            raise_image("IMG_REFERENCE_INVALID", f"参考图不存在：{asset_id}")
        assert isinstance(asset, ImageAsset)
        refs.append({"asset_id": asset.asset_id, "data": storage.get(asset.key),
                     "mime": asset.mime, "sha256": asset.sha256})
    return refs


async def execute_task(task_id: str) -> ImageTaskState:
    """无状态执行体：只依赖 task_id，任何实例都能跑（调度器负责并发与恢复）。"""
    settings = get_image_settings()
    registry = reg_mod.get_registry()
    registry.maybe_reload()

    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        if task is None:
            raise RuntimeError(f"任务不存在：{task_id}")
        if task.status in {"canceled", "done", "failed"}:
            return ImageTaskState(task_id=task_id)
        refs = _load_refs(session, list(task.ref_asset_ids or []))
        state = ImageTaskState(
            task_id=task.task_id, request_id=task.request_id, trace_id=task.trace_id,
            batch_id=task.batch_id or "", product_id=task.product_id,
            username=task.username, department=task.department, prompt=task.prompt,
            platform=task.platform, aspect=task.aspect, count=task.n,
            compositions=list(task.compositions or []),
            scene_styles=list(task.scene_styles or []),
            refs=[RefImage(asset_id=item["asset_id"], sha256=item["sha256"],
                           mime=item["mime"],
                           product_id=str(task.product_id or ""),
                           source=task.ref_source or "mcp") for item in refs],
            attrs=dict(task.attrs_json or {}),
            # 图位模板与 label 在建任务时已按中台 plan_policy 解析并落库：执行期不再回调中台
            slots=[SlotPlan(**item) for item in (task.slots_json or [])]
            or plan_slots.build_slots(ImageTaskState(count=task.n, aspect=task.aspect)),
        )

    state.touch("understand")
    if not state.attrs:
        # 中台数据缺失只在"历史任务/异常任务"出现：退回启发式，但记一条 warning（可观测）
        state.attrs = understand.understand(state)
        state.warnings.append("IMG_SCHEMA_INVALID: 任务缺少中台商品属性，已退回启发式抽取")
    state.touch("compose_prompt")
    fragments = _recall_for_slots(state, settings)
    state.prompt_pack = plan_slots.compose(state, registry, fragments=fragments)
    state.touch("validate")
    validate.degrade_note(state)
    validate.validate(state, settings=settings)
    # 只有 type=generate 的图位走出图通道；排版/实拍图位只登记规划（不产生图片、不计交付）
    generate_drafts = [d for d in state.prompt_pack if d.type == "generate"]
    if not generate_drafts:
        raise ImageError("IMG_INPUT_INVALID",
                         "所选图位都不需要 AI 出图（尺码对照/包装清单/售后排版请走设计排版）")
    state.prompt_final = generate_drafts[0].prompt or state.prompt
    if state.warnings:
        state.touch("validate_warnings", count=len(state.warnings))

    payload = {
        "provider": settings.provider,
        "refs": refs,
        "slots": [{"slot": d.slot, "prompt": d.prompt, "negative_prompt": d.negative_prompt,
                   "n": 1} for d in generate_drafts],
        "width": generate_drafts[0].width,
        "height": generate_drafts[0].height,
        "preserve": generate_drafts[0].preserve,
    }
    state.touch("generate")
    result = await generator_handler.run(payload)

    attempted = 0
    delivered = 0
    cost = 0.0
    outputs: list[dict] = []
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        if task is None or task.status == "canceled":
            return state
        for slot in result.get("slots") or []:
            for data in slot.get("images") or []:
                attempted += 1
                asset = asset_store.save_bytes(
                    session, data, kind="output", mime=slot.get("mime") or "image/png",
                    task_id=task_id, slot=int(slot.get("slot") or 1),
                    username=task.username, department=task.department,
                )
                delivered += 1 if asset is not None else 0
                if asset is not None:
                    # 只留纯数据：session 关闭后 ORM 对象不能再取属性（DetachedInstanceError）
                    outputs.append({
                        "slot": int(slot.get("slot") or 1),
                        "mime": asset.mime or slot.get("mime") or "image/png",
                        "sha256": asset.sha256, "width": asset.width,
                        "height": asset.height, "bytes": asset.bytes,
                        "storage": asset.storage, "key": asset.key,
                    })
            cost += float(slot.get("cost_cny") or 0.0)
        task_store.mark_done(session, task_id, attempted=attempted, delivered=delivered,
                             cost=cost, prompt_final=state.prompt_final,
                             prompt_pack=[d.model_dump() for d in state.prompt_pack],
                             slots=[s.model_dump() if hasattr(s, "model_dump") else dict(s)
                                    for s in (state.slots or [])])
    metrics.record_task_done(delivered=delivered, attempted=attempted)
    state.attempted_images = attempted
    state.delivered_images = delivered
    state.cost_cny = cost
    state.touch("aggregate", delivered=delivered)
    await _backfill(state, outputs_by_slot=outputs)
    return state


def _recall_for_slots(state: ImageTaskState, settings) -> dict[int, list[str]]:
    """按图位召回「写法片段」（RAG）：检索失败只记 warning，不影响出图。"""
    if not getattr(settings, "rag_enabled", True):
        return {}
    try:
        from app.agents_image.rag import index as rag_index

        product = str((state.attrs or {}).get("product_type") or "")
        top_k = int(getattr(settings, "rag_top_k", 2) or 2)
        fragments: dict[int, list[str]] = {}
        for item in state.slots or []:
            slot_no = int(getattr(item, "slot", 0) or 0)
            query = " ".join(part for part in (
                product, getattr(item, "role", ""),
                getattr(item, "composition_label", ""),
                getattr(item, "scene_style_label", ""),
            ) if part)
            hits = rag_index.retrieve(query, top_k=top_k)
            fragments[slot_no] = [hit.snippet for hit in hits]
        state.touch("retrieve", slots=len(fragments),
                    hits=sum(len(items) for items in fragments.values()))
        return fragments
    except Exception as exc:  # noqa: BLE001 —— RAG 是增强能力
        logger.warning("知识库召回失败（按无片段继续）：%s", exc)
        state.warnings.append(f"IMG_RAG_DEGRADED: 知识库召回失败（{type(exc).__name__}）")
        return {}


async def _backfill(state: ImageTaskState, *, outputs_by_slot: list[dict]) -> str:
    """结果元数据回写中台（**不含图片二进制**）；失败只改 `backfill_status`，不影响已交付的图。"""
    from app.agents_image.prompt_pack import pack_yaml
    from app.agents_image.sources.product_source import get_catalog

    generate_drafts = [draft for draft in state.prompt_pack if draft.type == "generate"]

    payload = {
        "request_id": state.request_id or state.task_id,
        "batch_id": state.batch_id,
        "product_id": state.product_id or "",
        "task_id": state.task_id,
        "platform": state.platform,
        "aspect": state.aspect,
        "count": state.count,
        "compositions": list(state.compositions or []),
        "scene_styles": list(state.scene_styles or []),
        "prompt_final": state.prompt_final,
        "prompt_pack": [draft.model_dump() for draft in state.prompt_pack],
        "params": {"provider": get_image_settings().provider,
                   "attempted": state.attempted_images, "delivered": state.delivered_images},
        "requester": state.username,
        "trace_id": state.trace_id,
        "prompt_pack_yaml": pack_yaml(state, generate_drafts),
        "results": [{
            "slot": item["slot"], "role": _role_of(state, item["slot"]),
            "prompt": _prompt_of(state, item["slot"]),
            "mime": item.get("mime") or "image/png",
            "sha256": item.get("sha256", ""),
            "width": item.get("width", 0),
            "height": item.get("height", 0),
            "bytes": item.get("bytes", 0),
            "storage": item.get("storage", "local"),
            "storage_key": item.get("key", ""),
            "cost_cny": 0.0, "status": "done",
        } for item in outputs_by_slot if item],
    }
    status = "failed"
    try:
        await get_catalog().save_result(payload)
        status = "sent"
    except Exception as exc:  # noqa: BLE001 —— 回写失败不能把已交付的图判死
        logger.warning("回写中台失败：task=%s %s", state.task_id, exc)
        state.warnings.append(f"IMG_BACKFILL_FAILED: 回写中台失败（{type(exc).__name__}），"
                              "图片已生成，等待重放")
    state.touch("backfill", status=status)
    try:
        with session_scope() as session:
            task_store.mark_backfill(session, state.task_id, status)
    except Exception:  # noqa: BLE001 —— 状态写失败不阻塞
        logger.warning("回写状态落库失败：task=%s", state.task_id)
    return status


def _role_of(state: ImageTaskState, slot: int) -> str:
    for item in state.slots or []:
        if int(getattr(item, "slot", 0) or 0) == slot:
            return getattr(item, "role", "") or ""
    return ""


def _prompt_of(state: ImageTaskState, slot: int) -> str:
    for draft in state.prompt_pack or []:
        if int(draft.slot or 0) == slot:
            return draft.prompt
    return ""


__all__ = ["execute_task"]
