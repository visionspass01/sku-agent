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
from app.agents_image.nodes import compliance, plan_slots, reflexion, understand, validate
from app.agents_image.nodes import critic as critic_mod
from app.agents_image.state import CriticResult, ImageTaskState, RefImage, SlotPlan
from app.agents_image.storage.image_storage import get_storage
from app.agents_image.store import assets as asset_store
from app.agents_image.store import memory as memory_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store.models import ImageAsset
from app.core.errors import ImageError
from app.db.engine import session_scope

logger = logging.getLogger("sku-agent.image.graph")


def _renew_lease(task_id: str, settings) -> None:
    """执行期续约（W1）：每个阶段续一次，保证在途任务不会被看门狗重投。"""
    seconds = int(getattr(settings, "task_lease_seconds", 300) or 300)
    try:
        with session_scope() as session:
            task_store.renew_lease(session, task_id, seconds=seconds)
    except Exception:  # noqa: BLE001 —— 续约失败不影响执行
        logger.warning("任务续约失败：task=%s", task_id)


def _load_refs(session, ref_ids: list[str]) -> list[dict]:
    storage = get_storage()
    refs: list[dict] = []
    for asset_id in ref_ids:
        asset = asset_store.get_asset(session, asset_id)
        if asset is None:
            raise ImageError("IMG_REFERENCE_INVALID", f"参考图不存在：{asset_id}")
        assert isinstance(asset, ImageAsset)
        try:
            data = storage.get(asset.key)
        except Exception as exc:  # noqa: BLE001 —— 行还在、文件被 24h 清理掉了
            raise ImageError(
                "IMG_REFERENCE_EXPIRED",
                "参考图已过保留期（本地存储 24 小时）被清理，请重新建任务"
                f"（asset={asset_id}）") from exc
        refs.append({"asset_id": asset.asset_id, "data": data,
                     "mime": asset.mime, "sha256": asset.sha256})
    return refs


async def _platform_rules(state: ImageTaskState):
    """取平台规则（图内文字/禁用词/大小限制）。

    P1-1：执行期与创建期保持**同一口径**——中台不可用就失败（不再 fail-open 用默认规则出图），
    否则同一套参数在两条路径上语义不一致，且可能用错平台的图内文字/白底规则。
    """
    from app.agents_image.sources.product_source import get_catalog

    return await get_catalog().get_platform_rules(platform=state.platform)


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
            reuse_asset_ids=dict(getattr(task, "reuse_asset_ids", {}) or {}),
            ref_source=task.ref_source or "mcp",
            # 图位模板与 label 在建任务时已按中台 plan_policy 解析并落库：执行期不再回调中台
            slots=[SlotPlan(**item) for item in (task.slots_json or [])]
            or plan_slots.build_slots(ImageTaskState(count=task.n, aspect=task.aspect)),
        )

    _renew_lease(task_id, settings)
    state.touch("understand")
    if not state.attrs:
        # 中台数据缺失只在"历史任务/异常任务"出现：退回启发式，但记一条 warning（可观测）
        state.attrs = understand.understand(state)
        state.warnings.append("IMG_SCHEMA_INVALID: 任务缺少中台商品属性，已退回启发式抽取")

    rules = await _platform_rules(state)
    policy = await _plan_policy(state)
    state.touch("compose_prompt")
    fragments = _recall_for_slots(state, settings)
    all_drafts = plan_slots.compose(state, registry, rules=rules, policy=policy,
                                    fragments=fragments)
    state.touch("validate")
    validate.degrade_note(state)
    validate.validate(state, settings=settings)

    generate_drafts = [draft for draft in all_drafts if draft.type == "generate"]
    # C2：iterate 复用 base 任务成品 —— 被复用的图位不再调用生图模型
    reuse_map = {int(slot): asset_id for slot, asset_id in (state.reuse_asset_ids or {}).items()}
    if reuse_map:
        generate_drafts = [draft for draft in generate_drafts if draft.slot not in reuse_map]
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
    provider_calls = 0
    for attempt in range(1, max_attempts + 1):
        state.attempt = attempt
        _renew_lease(task_id, settings)
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
        provider_calls += int(result.get("provider_calls") or len(images))
        attempted += len(images)
        _renew_lease(task_id, settings)
        state.touch("critic", attempt=attempt, images=len(images))
        critic = critic_mod.critique(state, current, images, settings=settings, rules=rules,
                                     manifest=registry.manifest)
        if critic.passed:
            break
        signature = critic_mod.failure_signature(state, critic)
        memory_patches = _memory_patches(signature)
        state.touch("reflexion", attempt=attempt, issues=len(critic.issues))
        if attempt >= max_attempts:
            _archive_failure(state, critic, signature, attempted,
                             cost=sum(float(item or 0.0) for item in image_costs))
            # 失败也要记账（使用量=已生成张数、成本已发生），并且只结算一次
            with session_scope() as session:
                _cancel_settle(session, state, attempted=attempted,
                               provider_calls=provider_calls,
                               cost=sum(float(item or 0.0) for item in image_costs))
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
    # 第三轮补充：像素门禁 + autofix（纯 Python 解码/重编码，单图可达数百毫秒）**在事务外**算完，
    # 事务里只做落盘入库，避免 8 个并发任务把 SQLite 写锁持有到超时（冒烟出现过 database is locked）。
    gate_mode = str(getattr(settings, "compliance_gate", "main") or "main")
    autofix_images = bool(getattr(settings, "compliance_autofix", True))
    nonpng_policy = str(getattr(settings, "compliance_nonpng", "warn") or "warn")
    gated: list[tuple[int, str, object, str]] = []
    for image in images:
        role = _role_of(state, image.slot)
        verdict = compliance.gate(
            image.data, slot=image.slot, role=role, platform_rules=rules,
            gate_mode=gate_mode, autofix=autofix_images, aspect=state.aspect,
            nonpng_policy=nonpng_policy,
        )
        gated.append((image.slot, role, verdict, image.mime or "image/png"))
    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        if task is None or task.status == "canceled":
            return state
        # 取消语义（P1-6）：provider 已返回但用户已请求取消 → 丢弃结果，不交付
        if getattr(task, "cancel_requested", False):
            task_store.update_task(session, task_id, status="canceled",
                                   finished_at=dt.datetime.now(),
                                   attempted_images=attempted,
                                   error_code="IMG_TASK_CANCELED",
                                   error_detail="已提交生图模型，结果被丢弃（用户取消）")
            _cancel_settle(session, state, attempted=attempted,
                           provider_calls=provider_calls,
                           cost=sum(float(item or 0.0) for item in image_costs))
            metrics.record_task_failed()
            state.touch("canceled", attempted=attempted)
            return state
        blocked: list[dict] = []
        delivered_slots: set[int] = set()
        gate_skipped = 0
        for slot, role, verdict, image_mime in gated:
            if verdict.degraded:
                gate_skipped += 1
                state.warnings.append(
                    "IMG_GATE_SKIPPED: 图位 "
                    f"{slot}（{role or '-'}）像素门禁被跳过（"
                    + ("；".join(verdict.reasons) or "非 PNG 且未安装 Pillow")
                    + "），该图按平台规则未做像素校验")
            if not verdict.passed:
                asset_store.save_bytes(
                    session, verdict.data, kind="blocked", mime=image_mime,
                    task_id=task_id, slot=slot,
                    username=task.username, department=task.department,
                    blocked_reason=("; ".join(verdict.reasons))[:60],
                )
                blocked.append({"slot": slot, "reasons": list(verdict.reasons)})
                state.warnings.append(
                    "IMG_CONTENT_BLOCKED: 图位 "
                    f"{slot}（{role or '-'}）像素门禁未通过：" + "; ".join(verdict.reasons))
                continue
            asset = asset_store.save_bytes(
                session, verdict.data, kind="output", mime=image_mime,
                task_id=task_id, slot=slot,
                username=task.username, department=task.department,
            )
            delivered += 1 if asset is not None else 0
            delivered_slots.add(int(slot))
            if asset is not None:
                # 只留纯数据：session 关闭后 ORM 对象不能再取属性（DetachedInstanceError）
                outputs.append({
                    "slot": slot, "mime": asset.mime or image_mime,
                    "sha256": asset.sha256, "width": asset.width,
                    "height": asset.height, "bytes": asset.bytes,
                    "storage": asset.storage, "key": asset.key,
                    "gate_fixed": bool(verdict.fixed),
                })
        # C2：被复用的图位把历史成品挂到本任务（不重复生成、不重复计费）
        reused = 0
        for slot, asset_id in (reuse_map or {}).items():
            base_asset = asset_store.get_asset(session, asset_id)
            if base_asset is None:
                continue
            # P1-4：建任务到执行之间可能被人工驳回/清理 —— 复用前后都要校验，
            # 被驳回的成品不许顺着复用链复活（跳过并留告警，该图位按差额补齐）
            if (str(getattr(base_asset, "adopt_status", "") or "") == "rejected"
                    or getattr(base_asset, "purged_at", None) is not None):
                state.warnings.append(
                    f"IMG_REUSE_REJECTED: 图位 {slot} 的复用源已被人工驳回或清理，"
                    "已跳过复用（该图位按差额补齐）")
                continue
            try:
                data = asset_store.read_asset_bytes(base_asset)
            except Exception as exc:  # noqa: BLE001 —— 历史成品已被 24h 清理
                state.warnings.append(
                    f"IMG_REUSE_EXPIRED: 图位 {slot} 的历史成品已过保留期，已跳过复用"
                    f"（{type(exc).__name__}）")
                continue
            asset = asset_store.save_bytes(
                session, data, kind="output", mime=base_asset.mime,
                task_id=task_id, slot=slot, username=task.username,
                department=task.department, source="reuse",
            )
            outputs.append({
                "slot": slot, "mime": asset.mime, "sha256": asset.sha256,
                "width": asset.width, "height": asset.height, "bytes": asset.bytes,
                "storage": asset.storage, "key": asset.key, "reused": True,
            })
            reused += 1
        if reused:
            task_store.update_task(session, task_id, reused_images=reused)
        if gate_skipped:
            metrics.record_gate_skipped(gate_skipped)
        if blocked and delivered == 0:
            reasons = "；".join(
                f"图位 {item['slot']}：{'、'.join(item['reasons'])}" for item in blocked[:2])
            # 全被门禁拦下 = 失败，但 provider 已经出过图：使用量/成本照记、额度退回（只结算一次）
            _cancel_settle(session, state, attempted=attempted,
                           provider_calls=provider_calls,
                           cost=sum(float(item or 0.0) for item in image_costs))
            raise ImageError("IMG_CONTENT_BLOCKED", f"所有出图都未通过平台像素门禁：{reasons}")
        # 部分成功（第三轮 P1-1）：交付张数少于计划时，必须说清"预期 X 张、输出 Y 张"
        expected = int(state.count or 0) or len(current)
        missing = sorted({int(draft.slot) for draft in current} - delivered_slots)
        partial = bool(missing) or delivered < expected
        state.expected_images = expected
        state.partial = partial
        if partial:
            state.warnings.append(
                f"IMG_SLOT_MISSING: 预期 {expected} 张，输出 {delivered} 张"
                + (f"（缺图位 {missing}）" if missing else ""))
            metrics.record_partial_delivery()
            logger.warning("部分交付：task=%s expected=%s delivered=%s missing=%s",
                           task_id, expected, delivered, missing)
        # 成本口径（P0-5）：一律用 **provider 返回的真实计费**，不再用配置单价估
        for item in image_costs:
            external_cost += float(item or 0.0)
        cost = external_cost
        copy_json = _copy_contract(state)
        task_store.mark_done(session, task_id, attempted=attempted, delivered=delivered,
                             cost=cost, prompt_final=state.prompt_final,
                             prompt_pack=[draft.model_dump() for draft in state.prompt_pack],
                             attempts=state.attempt, external_cost=external_cost,
                             slots=[slot.model_dump() if hasattr(slot, "model_dump") else dict(slot)
                                    for slot in (state.slots or [])])
        task_store.update_task(session, task_id, copy_json=copy_json,
                               provider_calls=provider_calls,
                               blocked_json=list(blocked or []))
        # 结算里会追加对账告警（IMG_QUOTA_SETTLE_MISMATCH），所以告警的落库放到结算之后
        _settle(session, state, delivered=delivered, attempted=attempted, cost=cost,
                provider_calls=provider_calls)
    metrics.record_task_done(delivered=delivered, attempted=attempted)
    state.attempted_images = attempted
    state.delivered_images = delivered
    state.cost_cny = cost
    state.touch("aggregate", delivered=delivered, attempts=state.attempt)
    await _backfill(state, outputs_by_slot=outputs, blocked=blocked)
    return state


def _archive_failure(state: ImageTaskState, critic: CriticResult, signature: str,
                     attempted: int, cost: float = 0.0) -> None:
    """失败归档：任务 attempts 落库 + 失败签名写进 `image_memory`（下次同因优先复用修复）。

    使用量/额度不在这里记：统一由 `settlement.settle_cancel()` 在调度器的失败分支结算
    （第三轮 P1-3：一处记账，避免失败被记两次）。
    """
    patch = "；".join(critic.issues[:3]) or "未知原因"
    try:
        with session_scope() as session:
            task_store.update_task(session, state.task_id, attempts=attempted,
                                   attempted_images=attempted, error_code="IMG_CRITIC_FAILED",
                                   error_detail=patch[:500])
            memory_store.remember(session, signature=signature, fix_summary=patch,
                                  prompt_patch=patch, department=state.department)
    except Exception:  # noqa: BLE001 —— 归档失败不改变任务结论
        logger.warning("失败归档写入出错：task=%s", state.task_id)


def _copy_contract(state: ImageTaskState) -> dict:
    """图内文案契约（W4）：任务详情与中台回写都用这一份，避免两边字段漂移。"""
    slots = []
    for draft in state.prompt_pack or []:
        spec = dict(getattr(draft, "copy_spec", None) or {})
        if not spec:
            continue
        slots.append({"slot": int(draft.slot or 0), "role": draft.role or "",
                      **spec})
    return {"platform": state.platform, "variant_id": state.variant_id,
            "variant_direction": state.variant_direction, "slots": slots}


def _settle(session, state: ImageTaskState, *, delivered: int, attempted: int,
            cost: float, provider_calls: int = 0) -> None:
    """交付结算（委托 `settlement.settle_done`）：使用量占额度、多预留释放、写告警。"""
    from app.agents_image import settlement

    settlement.settle_done(session, state.task_id, delivered=delivered,
                           attempted=attempted, cost=cost,
                           provider_calls=provider_calls, warnings=state.warnings)


def _cancel_settle(session, state: ImageTaskState, *, attempted: int,
                   provider_calls: int = 0, cost: float = 0.0) -> None:
    """取消/失败结算（委托 `settlement.settle_cancel`）：额度全退，使用量/成本照记。"""
    from app.agents_image import settlement

    settlement.settle_cancel(session, state.task_id, attempted=attempted,
                             provider_calls=provider_calls, cost=cost,
                             warnings=state.warnings)


async def _plan_policy(state: ImageTaskState):
    """取中台图位策略（含 copy_table / variant_directions）；不可用同样直接失败（P1-1）。"""
    from app.agents_image.sources.product_source import get_catalog

    return await get_catalog().get_plan_policy(platform=state.platform)


async def _backfill(state: ImageTaskState, *, outputs_by_slot: list[dict],
                   blocked: list[dict] | None = None) -> str:
    """结果元数据回写中台（**不含图片二进制**）；失败只改 `backfill_status`，不影响已交付的图。

    第三轮 P2-16：把"被门禁拦下的图位 / 告警 / 部分成功"也回写，中台侧才能解释为什么少了图。
    """
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
        "copy_json": _copy_contract(state),
        "ref_source": state.ref_source if hasattr(state, "ref_source") else "",
        "expected_images": int(getattr(state, "expected_images", 0) or 0),
        "partial": bool(getattr(state, "partial", False)),
        "warnings": list(state.warnings or []),
        "blocked": list(blocked or []),
        "params": {"provider": get_image_settings().provider,
                   "attempts": state.attempt, "attempted": state.attempted_images,
                   "delivered": state.delivered_images,
                   "expected": int(getattr(state, "expected_images", 0) or 0),
                   "partial": bool(getattr(state, "partial", False)),
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
            "reused": bool(item.get("reused")),
            "copy_json": _slot_copy_of(state, item["slot"]),
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
            # 回写阶段才产生的告警（IMG_BACKFILL_FAILED / IMG_OUTBOX_ENQUEUE_FAILED）也要落库，
            # 否则任务详情里永远看不到"回写失败、等待重放"（P1-3）；
            # 用 merge 而不是覆盖，建任务阶段的告警（如 IMG_REUSE_SKIPPED）不能被抹掉
            task_store.merge_warnings(session, state.task_id, state.warnings)
    except Exception:  # noqa: BLE001 —— 状态写失败不阻塞
        logger.warning("回写状态落库失败：task=%s", state.task_id)
    return status


def _enqueue_outbox(state: ImageTaskState, payload: dict) -> None:
    """回写失败进 outbox（重放扫它）；入队失败要留痕，否则这批结果就真的丢了。"""
    try:
        from app.agents_image.store import outbox as outbox_store

        with session_scope() as session:
            outbox_store.enqueue(session, task_id=state.task_id, payload=payload,
                                 error="backfill failed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("outbox 入队失败：task=%s %s", state.task_id, exc)
        state.warnings.append(
            f"IMG_OUTBOX_ENQUEUE_FAILED: 回写补偿入队失败（{type(exc).__name__}），"
            "本次结果元数据可能丢失，请人工核对中台")


def _role_of(state: ImageTaskState, slot: int) -> str:
    for item in state.slots or []:
        if int(getattr(item, "slot", 0) or 0) == slot:
            return getattr(item, "role", "") or ""
    return ""


def _slot_copy_of(state: ImageTaskState, slot: int) -> dict:
    for draft in state.prompt_pack or []:
        if int(draft.slot or 0) == int(slot):
            return dict(getattr(draft, "copy_spec", None) or {})
    return {}


def _prompt_of(state: ImageTaskState, slot: int) -> str:
    for draft in state.prompt_pack or []:
        if int(draft.slot or 0) == slot:
            return draft.prompt
    return ""


__all__ = ["execute_task"]
