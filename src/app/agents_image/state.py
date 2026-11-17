"""生图任务的图状态（LangGraph state）。

字段先定义完整（后续里程碑逐步填充），全部可 JSON 序列化，便于回放、评测与排障。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
    aspect: str = "1:1"
    count: int = 1
    compositions: list[str] = Field(default_factory=list)
    scene_styles: list[str] = Field(default_factory=list)

    refs: list[RefImage] = Field(default_factory=list)
    attrs: dict[str, Any] = Field(default_factory=dict)
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

    mcp_degraded: bool = False
    warnings: list[str] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""

    def touch(self, stage: str, **extra: Any) -> None:
        """记录阶段耗时（timeline 是排障与评测的唯一时间源）。"""
        self.timeline.append({"stage": stage, **extra})
