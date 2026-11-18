"""图生图编排：理解 → 召回 → 规划图位 → 组装 Prompt → 校验 →（生成 → 评审 → 反思）→ 落库 → 回写。

节点顺序（timeline 里可见）：
`understand → compose_prompt → retrieve → validate → generate → critic → (reflexion → generate → critic)*
 → aggregate → backfill`

两条硬边界：
- 只有 `type=generate` 的图位进入出图通道（尺码对照 / 包装清单 / 售后排版不生成图片）；
- Critic 不通过时**只重写对应图位的 Prompt**（Reflexion），轮数上限 `IMAGE_REFLEXION_MAX_ROUNDS`，
  超限判 `IMG_CRITIC_FAILED` 并把失败签名归档进 `image_memory`；交付张数只算最终通过那一轮。
"""

from __future__ import annotations

import datetime as dt
import logging

from app.agents_image import metrics
from app.agents_image import registry as reg_mod
from app.agents_image.config import get_image_settings
from app.agents_image.generator import handler as generator_handler
from app.agents_image.nodes import critic as critic_mod
from app.agents_image.nodes import plan_slots, reflexion, understand, validate
from app.agents_image.state import CriticResult, ImageTaskState, RefImage, SlotPlan
from app.agents_image.storage.image_storage import get_storage
from app.agents_image.store import assets as asset_store
from app.agents_image.store import memory as memory_store
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
            raise ImageError("IMG_REFERENCE_INVALID", f"参考图不存在：{asset_id}")
        assert isinstance(asset, ImageAsset)
        refs.append({"asset_id": asset.asset_id, "data": storage.get(asset.key),
                     "mime": asset.mime, "sha256": asset.sha256})
    return refs


async def _platform_rules(state: ImageTaskState):
    """执行期尽量取平台规则（图内文字/禁用词/大小限制）；中台不可用只记 warning，
    因为任务创建阶段已经校验过参数，这里失败不该让已交付的图作废。"""
    try:
        from app.agents_image.sources.product_source import get_catalog

        return await get_catalog().get_platform_rules(platform=state.platform)
    except Exception as exc:  # noqa: BLE001
        logger.info("执行期取平台规则失败（用 manifest 默认）：%s", exc)
        state.warnings.append(f"IMG_UPSTREAM_MCP_UNAVAILABLE: 执行期取平台规则失败（{type(exc).__name__}）")
        return None


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


def _collect_images(result: dict) -> list[critic_mod.CheckedImage]:
    images: list[critic_mod.CheckedImage] = []
    for slot in result.get("slots") or []:
        slot_no = int(slot.get("slot") or 1)
        mime = slot.get("mime") or "image/png"
        for data in slot.get("images") or []:
            images.append(critic_mod.CheckedImage(slot=slot_no, data=data, mime=mime,
                                                  meta=slot.get("meta") or {}))
    return images


def _memory_patches(signature: str) -> dict[str, str]:
    try:
        with session_scope() as session:
            return memory_store.patches_for(session, signature)
    except Exception:  # noqa: BLE001 —— 记忆读失败不影响出图
        return {}


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

    rules = await _platform_rules(state)
    state.touch("compose_prompt")
    fragments = _recall_for_slots(state, settings)
    all_drafts = plan_slots.compose(state, registry, rules=rules, fragments=fragments)
    state.touch("validate")
    validate.degrade_note(state)
    validate.validate(state, settings=settings)

    generate_drafts = [draft for draft in all_drafts if draft.type == "generate"]
    # 出图张数（image_count）可以小于 generate 图位数：只出前 N 个，其余图位留空给人工
    if state.count and len(generate_drafts) > int(state.count):
        generate_drafts = generate_drafts[: int(state.count)]
    static_drafts = [draft for draft in all_drafts if draft.type != "generate"]
    if not generate_drafts:
        raise ImageError("IMG_INPUT_INVALID",
                         "所选图位都不需要 AI 出图（尺码对照/包装清单/售后排版请走设计排版）")

    max_attempts = max(1, int(getattr(settings, "reflexion_max_rounds", 2) or 0) + 1)
    current = [draft.model_copy(deep=True) for draft in generate_drafts]
    images: list[critic_mod.CheckedImage] = []
    image_costs: list[float] = []
    critic = CriticResult()
    attempted = 0
    for attempt in range(1, max_attempts + 1):
        state.attempt = attempt
        state.touch("generate", attempt=attempt, slots=len(current))
        # provider 调用标记：取消 API 据此判断"能不能撤回"（P1-6）
        with session_scope() as session:
            task_store.update_task(session, task_id,
                                   provider_started_at=dt.datetime.now())
        result = await generator_handler.run({
            "provider": settings.provider,
            "refs": refs,
            "slots": [{"slot": draft.slot, "prompt": draft.prompt,
                       "negative_prompt": draft.negative_prompt, "n": 1} for draft in current],
            "width": current[0].width,
            "height": current[0].height,
            "preserve": current[0].preserve,
        })
        image_costs = [float(slot.get("cost_cny") or 0.0)
                       for slot in (result.get("slots") or [])]
        images = _collect_images(result)
        attempted += len(images)
        state.touch("critic", attempt=attempt, images=len(images))
        critic = critic_mod.critique(state, current, images, settings=settings, rules=rules)
        if critic.passed:
            break
        signature = critic_mod.failure_signature(state, critic)
        memory_patches = _memory_patches(signature)
        state.touch("reflexion", attempt=attempt, issues=len(critic.issues))
        if attempt >= max_attempts:
            _archive_failure(state, critic, signature, attempted)
            raise ImageError("IMG_CRITIC_FAILED",
                             f"评审未通过（第 {attempt} 轮，score={critic.score}）："
                             f"{'；'.join(critic.issues[:3])}")
        reflection = reflexion.reflect(state, critic, current, rules=rules,
                                       memory=memory_patches)
        current = reflexion.apply_patches(current, reflection)

    state.prompt_pack = current + static_drafts
    state.critic = critic
    state.prompt_final = current[0].prompt or state.prompt
    if state.warnings:
        state.touch("validate_warnings", count=len(state.warnings))

    delivered = 0
    cost = 0.0
    external_cost = 0.0
    outputs: list[dict] = []
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        if task is None or task.status == "canceled":
            return state
        # 取消语义（P1-6）：provider 已返回但用户已请求取消 → 丢弃结果，不交付
        if getattr(task, "cancel_requested", False):
            task_store.update_task(session, task_id, status="canceled",
                                   finished_at=dt.datetime.now(),
                                   attempted_images=attempted,
                                   error_code="IMG_TASK_CONFLICT",
                                   error_detail="已提交生图模型，结果被丢弃（用户取消）")
            _cancel_settle(session, state, attempted=attempted)
            metrics.record_task_failed()
            state.touch("canceled", attempted=attempted)
            return state
        for image in images:
            asset = asset_store.save_bytes(
                session, image.data, kind="output", mime=image.mime or "image/png",
                task_id=task_id, slot=image.slot,
                username=task.username, department=task.department,
            )
            delivered += 1 if asset is not None else 0
            if asset is not None:
                # 只留纯数据：session 关闭后 ORM 对象不能再取属性（DetachedInstanceError）
                outputs.append({
                    "slot": image.slot, "mime": asset.mime or image.mime or "image/png",
                    "sha256": asset.sha256, "width": asset.width,
                    "height": asset.height, "bytes": asset.bytes,
                    "storage": asset.storage, "key": asset.key,
                })
        # 成本口径（P0-5）：一律用 **provider 返回的真实计费**，不再用配置单价估
        for item in image_costs:
            external_cost += float(item or 0.0)
        cost = external_cost
        task_store.mark_done(session, task_id, attempted=attempted, delivered=delivered,
                             cost=cost, prompt_final=state.prompt_final,
                             prompt_pack=[draft.model_dump() for draft in state.prompt_pack],
                             attempts=state.attempt, external_cost=external_cost,
                             slots=[slot.model_dump() if hasattr(slot, "model_dump") else dict(slot)
                                    for slot in (state.slots or [])])
        _settle(session, state, delivered=delivered, attempted=attempted, cost=cost)
    metrics.record_task_done(delivered=delivered, attempted=attempted)
    state.attempted_images = attempted
    state.delivered_images = delivered
    state.cost_cny = cost
    state.touch("aggregate", delivered=delivered, attempts=state.attempt)
    await _backfill(state, outputs_by_slot=outputs)
    return state


def _archive_failure(state: ImageTaskState, critic: CriticResult, signature: str,
                     attempted: int) -> None:
    """失败归档：任务 attempts 落库 + 失败签名写进 `image_memory`（下次同因优先复用修复）。`r`n`r`n    同时释放预留给本次任务的额度（Critic 超限 = 一张都没交付）。`r`n    """
    patch = "；".join(critic.issues[:3]) or "未知原因"
    try:
        with session_scope() as session:
            task_store.update_task(session, state.task_id, attempts=attempted,
                                   attempted_images=attempted, error_code="IMG_CRITIC_FAILED",
                                   error_detail=patch[:500])
            memory_store.remember(session, signature=signature, fix_summary=patch,
                                  prompt_patch=patch, department=state.department)
            from app.agents_image.store import usage as usage_store

            usage_store.record(session, username=state.username, department=state.department,
                               failed=1, tasks=1, attempted=attempted)
    except Exception:  # noqa: BLE001 —— 归档失败不改变任务结论
        logger.warning("失败归档写入出错：task=%s", state.task_id)


def _settle(session, state: ImageTaskState, *, delivered: int, attempted: int,
            cost: float) -> None:
    """结算：把预留转成已用（只算成功出图张数），多预留的部分释放；再记使用量流水。"""
    from app.agents_image.store import quota as quota_store
    from app.agents_image.store import usage as usage_store
    from app.agents_image.store import users as user_store

    try:
        row = user_store.get_user(session, state.username)
        is_admin = bool(row is not None and (row.role or "") == "admin")
    except Exception:  # noqa: BLE001 —— 账号查不到按普通用户处理
        is_admin = False
    planned = _planned_of(session, state.task_id)
    if not is_admin:
        # 交付成功张数结算，未出图的预留全部释放（失败/取消同理走 _cancel_settle）
        quota_store.commit_reserve(session, state.username, min(delivered, planned))
        quota_store.release(session, state.username, max(0, planned - delivered))
    usage_store.record(session, username=state.username, department=state.department,
                       delivered=delivered, attempted=attempted, tasks=1, cost=cost)


def _cancel_settle(session, state: ImageTaskState, *, attempted: int) -> None:
    """取消/失败结算：预留全部释放，尝试张数照记（成本已在 provider 侧发生）。"""
    from app.agents_image.store import quota as quota_store
    from app.agents_image.store import usage as usage_store
    from app.agents_image.store import users as user_store

    try:
        row = user_store.get_user(session, state.username)
        is_admin = bool(row is not None and (row.role or "") == "admin")
    except Exception:  # noqa: BLE001
        is_admin = False
    planned = _planned_of(session, state.task_id)
    if not is_admin and planned:
        quota_store.release(session, state.username, planned)
    usage_store.record(session, username=state.username, department=state.department,
                       failed=1, tasks=1, attempted=attempted)


def _planned_of(session, task_id: str) -> int:
    task = task_store.get_task(session, task_id)
    if task is None:
        return 0
    return int(getattr(task, "image_count", 0) or getattr(task, "n", 0) or 0)


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
        "prompt_pack_yaml": pack_yaml(state, generate_drafts),
        "params": {"provider": get_image_settings().provider,
                   "attempts": state.attempt, "attempted": state.attempted_images,
                   "delivered": state.delivered_images,
                   "critic_score": state.critic.score,
                   "cost_cny": state.cost_cny},
        "requester": state.username,
        "trace_id": state.trace_id,
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
            "critic_score": state.critic.score,
            "cost_cny": round(state.cost_cny / max(1, len(outputs_by_slot)), 6) if outputs_by_slot else 0.0,
            "status": "done",
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
        _enqueue_outbox(state, payload)
    state.touch("backfill", status=status)
    try:
        with session_scope() as session:
            task_store.mark_backfill(session, state.task_id, status)
    except Exception:  # noqa: BLE001 —— 状态写失败不阻塞
        logger.warning("回写状态落库失败：task=%s", state.task_id)
    return status


def _enqueue_outbox(state: ImageTaskState, payload: dict) -> None:
    """回写失败进 outbox（D12 的重放扫它）；这里只做最小投递，失败只记日志。"""
    try:
        from app.agents_image.store import outbox as outbox_store

        with session_scope() as session:
            outbox_store.enqueue(session, task_id=state.task_id, payload=payload,
                                 error="backfill failed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("outbox 入队失败：task=%s %s", state.task_id, exc)


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
