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
    state.prompt_pack = plan_slots.compose(state, registry)
    state.touch("validate")
    validate.degrade_note(state)
    validate.validate(state, settings=settings)
    state.prompt_final = state.prompt_pack[0].prompt if state.prompt_pack else state.prompt
    if state.warnings:
        state.touch("validate_warnings", count=len(state.warnings))

    payload = {
        "provider": settings.provider,
        "refs": refs,
        "slots": [{"slot": d.slot, "prompt": d.prompt, "negative_prompt": d.negative_prompt,
                   "n": 1} for d in state.prompt_pack],
        "width": state.prompt_pack[0].width if state.prompt_pack else 1024,
        "height": state.prompt_pack[0].height if state.prompt_pack else 1024,
        "preserve": state.prompt_pack[0].preserve if state.prompt_pack else [],
    }
    state.touch("generate")
    result = await generator_handler.run(payload)

    attempted = 0
    delivered = 0
    cost = 0.0
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
    return state


__all__ = ["execute_task"]
