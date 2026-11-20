"""生图任务的图状态（LangGraph state）。

字段先定义完整（后续里程碑逐步填充），全部可 JSON 序列化，便于回放、评测与排障。
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr


class RefImage(BaseModel):
    """参考图（v6：一律来自中台商品图，用户不可上传）。"""

    asset_id: str
    sha256: str = ""
    mime: str = ""
    width: int = 0
    height: int = 0
    bytes: int = 0
    source: str = "mcp"          # 只可能是 mcp
    product_id: str = ""
    image_id: str = ""
    color: str = ""
    color_hex: str = ""


class SlotPlan(BaseModel):
    """一个图位的规划：模板来自中台 `plan_policy`，label 在建任务时已解析好（执行期不回调中台）。"""

    slot: int = Field(ge=1, le=9)
    role: str = ""
    composition_id: str = ""
    composition_label: str = ""
    scene_style_id: str = ""
    scene_style_label: str = ""
    type: str = "generate"     # generate | layout | photo_required


class PromptDraft(BaseModel):
    slot: int = Field(ge=1, le=9)
    role: str = ""
    prompt: str = ""
    negative_prompt: str = ""
    aspect: str = "1:1"
    width: int = 1024
    height: int = 1024
    preserve: list[str] = Field(default_factory=list)
    type: str = "generate"        # generate | layout | photo_required
    fill_percent: int = 70        # 铁律②：商品占比数字化
    margin_percent: int = 8       # 铁律③：留白显式声明
    text_policy: str = "none"     # none | headline | spec
    fragments: list[str] = Field(default_factory=list)   # RAG 命中的写法片段
    copy_spec: dict = Field(default_factory=dict)        # 图内文案（headline/subhead/badge/字数）


class GeneratedImage(BaseModel):
    slot: int = 1
    asset_id: str = ""
    sha256: str = ""
    mime: str = "image/png"
    width: int = 0
    height: int = 0
    bytes: int = 0
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    storage: str = "local"
    key: str = ""


class CriticResult(BaseModel):
    passed: bool = False
    score: int = 0
    issues: list[str] = Field(default_factory=list)
    consistency: dict[str, Any] = Field(default_factory=dict)
    skipped: bool = False


class ImageTaskState(BaseModel):
    request_id: str = ""
    task_id: str = ""
    batch_id: str = ""
    product_id: str | None = None
    trace_id: str = ""
    username: str = "anonymous"
    department: str | None = None

    prompt: str = ""
    prompt_final: str = ""
    platform: str = "淘系"
    variant_id: str = ""          # A/B（同款多店铺差异化）
    variant_direction: str = ""   # 该变体的主图方向（如"版型优先"）
    ref_source: str = "mcp"       # 参考图来源（mcp / library / pailitao，W3）
    aspect: str = "1:1"
    count: int = 1
    compositions: list[str] = Field(default_factory=list)
    scene_styles: list[str] = Field(default_factory=list)

    refs: list[RefImage] = Field(default_factory=list)
    attrs: dict[str, Any] = Field(default_factory=dict)
    reuse_asset_ids: dict[str, str] = Field(default_factory=dict)  # C2：slot→历史成品
    intent: dict[str, Any] = Field(default_factory=dict)
    slots: list[SlotPlan] = Field(default_factory=list)
    prompt_pack: list[PromptDraft] = Field(default_factory=list)

    images: list[GeneratedImage] = Field(default_factory=list)
    critic: CriticResult = Field(default_factory=CriticResult)
    reflections: list[str] = Field(default_factory=list)
    attempt: int = 0
    max_attempts: int = 2

    attempted_images: int = 0
    delivered_images: int = 0
    cost_cny: float = 0.0
    # 第七轮 P1-1：LLM/视觉解析的用量与成本（与生图成本分开记，总成本 = 两者相加）
    llm_calls: int = 0
    llm_tokens: int = 0
    llm_cost_cny: float = 0.0
    # 部分成功（第三轮 P1-1）：预期张数与"是否部分交付"，用于详情/SSE/回写统一口径
    expected_images: int = 0
    partial: bool = False

    mcp_degraded: bool = False
    warnings: list[str] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""

    _last_touch: float | None = PrivateAttr(default=None)
    _last_stage: str | None = PrivateAttr(default=None)

    def touch(self, stage: str, **extra: Any) -> None:
        """记录阶段耗时（timeline 是排障与评测的唯一时间源）。

        第六轮 P1-7：直方图必须记**上一阶段**的耗时。
        上一版在 `touch(stage)` 时把「上次 touch 到现在的耗时」直接打进**本阶段**标签，
        导致所有阶段整体错位一格（generate 的耗时被算到 critic 头上），用它排查"谁慢"会得出
        相反结论。现在把耗时记到 `prev_stage`，本阶段只负责"开始"。
        """
        now = time.perf_counter()
        last_ts, last_stage = self._last_touch, self._last_stage
        elapsed_ms = (now - last_ts) * 1000.0 if last_ts is not None else 0.0
        self._last_touch, self._last_stage = now, stage
        if last_ts is not None and last_stage:
            try:
                from app.agents_image import metrics as image_metrics

                image_metrics.record_stage(last_stage, elapsed_ms)
            except Exception:  # noqa: BLE001 —— 度量失败不影响主流程
                pass
        self.timeline.append({"stage": stage, "ms": int(elapsed_ms),
                              "prev_stage": last_stage or "", **extra})
