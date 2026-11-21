"""worker 主流程：取参考图 → 调第三方 → 校验/像素统计/门禁 → 落存储 → 返回路径。

职责边界（与父进程的分工）：
- **worker 只产出客观事实**：图片字节、尺寸、sha、像素统计、门禁结论、真实成本/发起量；
- **不做出图业务决策**（Critic 的 identity/aspect/banned 判定、Reflexion 重写、交付判定）：
  那些留在父进程，父进程直接消费这里返回的 `pixel` / `gate`，不必再下载图片；
- **不碰数据库**：路径由父进程登记资产并回写中台（`/api/v1/image/results`）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any

from app.agents_image.worker.contract import (
    SlotGate,
    SlotPixel,
    WorkerError,
    WorkerImage,
    WorkerRequest,
    WorkerResult,
)
from app.agents_image.worker.storage import (
    encode_for_transport,
    get_storage,
    sha256_bytes,
)
from app.core.errors import ImageError, normalize

logger = logging.getLogger("sku-agent.image.worker.runner")


def _rules_namespace(rules: Any) -> SimpleNamespace:
    """把契约里的规则包成 `compliance` 期望的属性对象（它只用到 getattr）。"""
    return SimpleNamespace(
        min_px=int(getattr(rules, "min_px", 800) or 800),
        max_mb=float(getattr(rules, "max_mb", 3.0) or 3.0),
        aspect=str(getattr(rules, "aspect", "1:1") or "1:1"),
        text_allowed=bool(getattr(rules, "text_allowed", True)),
        forbidden_words=list(getattr(rules, "forbidden_words", []) or []),
        compliance=dict(getattr(rules, "compliance", {}) or {}),
    )


def _pixel_of(decoded: tuple | None, *, white: float = 0.0, subject: float = 0.0,
              margin: float = 0.0) -> SlotPixel:
    pixel = SlotPixel(white_ratio=round(float(white), 4),
                      subject_ratio=round(float(subject), 4),
                      margin_ratio=round(float(margin), 4))
    if decoded is None:
        return pixel
    width, height, channels, pixels = decoded
    pixel.width, pixel.height, pixel.pixel_check = int(width), int(height), True
    try:
        from app.agents_image.pixels import dominant_color

        got = dominant_color(width, height, channels, pixels)
        if got is not None:
            pixel.dominant_rgb = [int(got[0]), int(got[1]), int(got[2])]
    except Exception:  # noqa: BLE001 —— 主色算不出不影响交付（父进程按"无漂移证据"处理）
        logger.debug("dominant_color 计算失败（忽略）")
    return pixel


async def _persist(storage: Any, data: bytes, *, kind: str, mime: str, task_id: str,
                   slot: int, prefix: str):  # noqa: ANN001, ANN202 —— StoredObject
    """同步后端（memory/cos）丢线程池，异步后端（platform）直接 await。"""
    if hasattr(storage, "put_async"):
        return await storage.put_async(data, kind=kind, mime=mime, task_id=task_id,
                                       slot=slot, prefix=prefix)
    return await asyncio.to_thread(storage.put, data, kind=kind, mime=mime,
                                   task_id=task_id, slot=slot, prefix=prefix)


async def execute(request: WorkerRequest, *, storage: Any = None,
                  settings: Any = None) -> WorkerResult:
    """执行一次"出图 + 落存储"；失败时也把**已发生的事实**（发起量/张数/成本）带回。"""
    from app.agents_image.config import get_image_settings
    from app.agents_image.generator import handler as generator_handler

    settings = settings or get_image_settings()
    backend = request.storage.backend or None
    storage = storage or get_storage(backend or "")
    timings: dict[str, int] = {}
    started = time.perf_counter()

    # 1) 参考图
    from app.agents_image.worker.refs import load_refs

    refs_started = time.perf_counter()
    refs = await load_refs(
        list(request.refs), storage=storage,
        timeout=float(getattr(settings, "mcp_timeout_seconds", 5.0) or 5.0),
        concurrency=int(getattr(settings, "refs_download_concurrency", 4) or 4),
        budget_seconds=float(getattr(settings, "refs_download_budget_seconds", 20.0) or 20.0))
    timings["refs_ms"] = int((time.perf_counter() - refs_started) * 1000)

    # 2) 调第三方（沿用 handler 的重试/发起量/截止时间语义）
    payload = {
        "provider": request.provider or getattr(settings, "provider", "fake") or "fake",
        "refs": refs,
        "slots": [{"slot": item.slot, "prompt": item.prompt,
                   "negative_prompt": item.negative_prompt, "n": item.n,
                   "seed": item.seed} for item in request.slots],
        "width": request.width, "height": request.height,
        "preserve": list(request.preserve or []),
        "deadline_at": float(request.deadline_at or 0.0),
    }
    provider_started = time.perf_counter()
    try:
        generated = await generator_handler.run(payload)
    except BaseException as exc:  # noqa: BLE001 —— 失败也要回传已发生的事实
        err = normalize(exc)
        stats = generator_handler.failure_stats_of(exc)
        timings["provider_ms"] = int((time.perf_counter() - provider_started) * 1000)
        logger.warning("worker 出图失败：%s（task=%s）", err.code, request.task_id)
        return WorkerResult(
            task_id=request.task_id, ok=False,
            provider_calls=int(stats.get("provider_calls") or 0),
            generated=int(stats.get("generated") or 0),
            cost_cny=float(stats.get("cost_cny") or 0.0),
            error=WorkerError(code=err.code, detail=err.detail,
                              retryable=bool(err.retryable)),
            timings=timings)
    timings["provider_ms"] = int((time.perf_counter() - provider_started) * 1000)

    # 3) 逐图：校验 → 像素统计 → 门禁 → 落存储
    from app.agents_image import pixels
    from app.agents_image.nodes import compliance

    gate_enabled = request.gate.mode != "off"
    rules = _rules_namespace(request.rules)
    images: list[WorkerImage] = []
    persist_started = time.perf_counter()
    try:
        for slot_result in generated.get("slots") or []:
            slot_no = int(slot_result.get("slot") or 1)
            mime = slot_result.get("mime") or "image/png"
            for raw in slot_result.get("images") or []:
                data = bytes(raw or b"")
                if not data:
                    continue
                decoded = pixels.decode_any(data)
                verdict = compliance.gate(
                    data, slot=slot_no, role=_role_of(request, slot_no),
                    platform_rules=rules, gate_mode="all" if gate_enabled else "off",
                    autofix=bool(request.gate.autofix),
                    aspect=request.gate.aspect or request.rules.aspect,
                    nonpng_policy=request.gate.nonpng_policy,
                    decoded=decoded) if gate_enabled else compliance.Verdict(
                    passed=True, reasons=[], data=data)
                blocked = gate_enabled and not verdict.passed
                stored = await _persist(
                    storage, verdict.data or data,
                    kind="blocked" if blocked else "output", mime=mime,
                    task_id=request.task_id, slot=slot_no,
                    prefix=request.storage.prefix)
                stats = dict(verdict.stats or {})
                pixel = _pixel_of(decoded,
                                  white=float(stats.get("white_ratio") or 0.0),
                                  subject=float(stats.get("subject_ratio") or 0.0),
                                  margin=float(stats.get("margin_ratio") or 0.0))
                images.append(WorkerImage(
                    slot=slot_no, kind="blocked" if blocked else "output",
                    sha256=stored.sha256 or sha256_bytes(data), mime=stored.mime,
                    width=stored.width, height=stored.height, bytes=stored.bytes,
                    storage=stored.storage, storage_key=stored.key, url=stored.url,
                    url_expire_at=stored.url_expire_at,
                    gate=SlotGate(passed=bool(verdict.passed),
                                  reasons=list(verdict.reasons or []),
                                  fixed=bool(verdict.fixed),
                                  degraded=bool(verdict.degraded), stats=stats),
                    pixel=pixel,
                    blocked_reason=("; ".join(verdict.reasons or []))[:60] if blocked else "",
                    cost_cny=float(slot_result.get("cost_cny") or 0.0),
                    latency_ms=int(slot_result.get("latency_ms") or 0),
                    content=(encode_for_transport(verdict.data or data)
                             if request.return_bytes else "")))
    except ImageError as exc:
        logger.warning("worker 落存储失败：%s（task=%s）", exc.code, request.task_id)
        timings["persist_ms"] = int((time.perf_counter() - persist_started) * 1000)
        timings["total_ms"] = int((time.perf_counter() - started) * 1000)
        return WorkerResult(
            task_id=request.task_id, ok=False,
            provider_calls=int(generated.get("provider_calls") or 0),
            generated=sum(len(item.get("images") or [])
                          for item in generated.get("slots") or []),
            cost_cny=sum(float(item.get("cost_cny") or 0.0)
                         for item in generated.get("slots") or []),
            error=WorkerError(code=exc.code, detail=exc.detail,
                              retryable=bool(exc.retryable)),
            timings=timings)
    timings["persist_ms"] = int((time.perf_counter() - persist_started) * 1000)
    timings["total_ms"] = int((time.perf_counter() - started) * 1000)

    generated_count = sum(len(item.get("images") or [])
                          for item in generated.get("slots") or [])
    cost = sum(float(item.get("cost_cny") or 0.0) for item in generated.get("slots") or [])
    # 4) 截止时间已过：图已生成但按"超时即失败"处理（成本照记，结果不交付）
    if request.deadline_at and time.time() > float(request.deadline_at):
        return WorkerResult(
            task_id=request.task_id, ok=False,
            provider_calls=int(generated.get("provider_calls") or 0),
            generated=generated_count, cost_cny=cost,
            error=WorkerError(code="IMG_TIMEOUT",
                              detail="任务超过最大等待时长（worker 侧截止）",
                              retryable=False),
            timings=timings)

    return WorkerResult(
        task_id=request.task_id, ok=True, images=images,
        provider_calls=int(generated.get("provider_calls") or 0),
        generated=generated_count, cost_cny=cost, timings=timings)


def _role_of(request: WorkerRequest, slot: int) -> str:
    for item in request.slots:
        if int(item.slot) == int(slot):
            return item.role or ""
    return ""


def result_to_json(result: WorkerResult) -> dict:
    """给 HTTP/SCF 用：`bytes` 已转成 base64 字符串，整份可直接 JSON 序列化。"""
    return result.model_dump(mode="json")


__all__ = ["execute", "result_to_json"]
