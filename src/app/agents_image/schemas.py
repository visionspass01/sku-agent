"""结构化 schema：服装属性（身份锁定）与生图意图。

两条原则：
1. **模型输出不合 schema 不该让整单失败**：`parse_attrs()` / `parse_intent()` 解析失败时
   退回启发式结果并在 `warnings` 里留一条 `IMG_SCHEMA_INVALID`（可观测、可评测），
   只有「人工提交的参数明显非法」才由 `nodes/validate.py` 抛错。
2. **身份锁定字段必须有值**：`must_not_change` 为空时补默认值，任何环节都不许把它清空
   —— 图生图的第一性要求是"别改商品"。
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

HEX_PATTERN = r"^#[0-9A-Fa-f]{6}$"
DEFAULT_MUST_NOT_CHANGE = ["颜色", "版型", "图案", "logo 文字", "关键部件位置"]
MAX_FEATURES = 8
MAX_FEATURE_LEN = 40


class ClothingAttrs(BaseModel):
    """参考图里商品的固定属性（Prompt 的商品槽位 + 身份锁定的依据）。"""

    model_config = ConfigDict(extra="ignore")   # 模型多给字段不算错

    product_type: str = "服装商品"
    category: Literal["男装", "女装", "童装", "通用"] = "通用"
    shape: str = "以参考图为准"
    color_hex: str = Field(default="#FFFFFF", pattern=HEX_PATTERN)
    color_names: list[str] = Field(default_factory=list)
    logo_text: str = "无"
    materials: str = "以参考图为准"
    key_features: list[str] = Field(default_factory=list)
    must_not_change: list[str] = Field(default_factory=lambda: list(DEFAULT_MUST_NOT_CHANGE))
    source: Literal["llm", "vision", "heuristic", "platform", "manual"] = "heuristic"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str = ""

    @field_validator("product_type", "shape", "logo_text", "materials", "notes")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @field_validator("key_features")
    @classmethod
    def _clean_features(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value or []:
            text = re.sub(r"\s+", " ", str(item or "")).strip()
            if text and text not in out:
                out.append(text[:MAX_FEATURE_LEN])
        return out[:MAX_FEATURES]

    @field_validator("must_not_change")
    @classmethod
    def _clean_locked_fields(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value or []:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
        return out or list(DEFAULT_MUST_NOT_CHANGE)

    def prompt_block(self) -> str:
        """商品槽位的文字（compose 直接拼进模板，保持一处措辞）。"""
        colors = "、".join(self.color_names) or self.color_hex
        return (
            f"{self.product_type}（{self.category}）；版型：{self.shape}；主色：{colors}；"
            f"材质：{self.materials}；logo/文字：{self.logo_text}"
        )


class ImageIntent(BaseModel):
    """一次生图的意图：要什么图、几张、什么比例、投哪个平台。"""

    model_config = ConfigDict(extra="ignore")

    goal: Literal["main", "detail", "scene", "size_chart", "package", "after_sale",
                   "custom"] = "main"
    aspect: Literal["1:1", "3:4", "4:3", "16:9"] = "1:1"
    count: int = Field(default=1, ge=1, le=9)
    platform: str = "淘系"
    compositions: list[str] = Field(default_factory=list)
    scene_styles: list[str] = Field(default_factory=list)
    audience: str = ""
    constraints: list[str] = Field(default_factory=list)
    source: Literal["llm", "heuristic", "platform", "manual"] = "manual"

    @field_validator("compositions", "scene_styles", "constraints")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for item in value or []:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
        return out


# ---------------------------------------------------------------- 容错解析
def _extract_json(raw: dict | str | None) -> dict:
    """从模型输出里捞 JSON 对象：容忍 ```json 围栏、前后解释性文字。"""
    if isinstance(raw, dict):
        return raw
    text = (raw or "").strip()
    if not text:
        raise ValueError("空输出")
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("找不到 JSON 对象")
        text = text[start:end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层必须是对象")
    return data


def parse_attrs(raw: dict | str | None, *, fallback: dict | None = None
                ) -> tuple[ClothingAttrs, list[str]]:
    """解析服装属性；失败退回 `fallback`（通常是启发式结果）+ 一条降级 warning。

    `raw` 为空（None/空串）表示"本次没有模型输出"——这是正常形态，不算降级，不报警。
    """
    base = dict(fallback or {})
    if _is_blank(raw):
        return _validated_or_default(ClothingAttrs, base, "heuristic"), []
    try:
        payload = _extract_json(raw)
        merged = {**base, **payload}
        return ClothingAttrs.model_validate(merged), []
    except Exception as exc:  # noqa: BLE001 —— JSON 坏了 / 字段类型不对，都走降级
        attrs = _validated_or_default(ClothingAttrs, {**base, "source": "heuristic"}, "heuristic")
        return attrs, [f"IMG_SCHEMA_INVALID: 服装属性解析失败（{type(exc).__name__}: {exc}），"
                       "已退回启发式结果"]


def parse_intent(raw: dict | str | None, *, fallback: dict | None = None
                 ) -> tuple[ImageIntent, list[str]]:
    """解析生图意图；失败退回 `fallback`（通常是请求参数）+ 一条降级 warning。"""
    base = dict(fallback or {})
    if _is_blank(raw):
        return _validated_or_default(ImageIntent, base, "manual"), []
    try:
        payload = _extract_json(raw)
        merged = {**base, **payload}
        return ImageIntent.model_validate(merged), []
    except Exception as exc:  # noqa: BLE001
        return _validated_or_default(ImageIntent, base, "manual"), [
            f"IMG_SCHEMA_INVALID: 生图意图解析失败（{type(exc).__name__}: {exc}），已退回请求参数"]


def _is_blank(raw: dict | str | None) -> bool:
    if raw is None:
        return True
    if isinstance(raw, str):
        return not raw.strip()
    return not raw


def _validated_or_default(model, payload: dict, source: str):
    """兜底值自身都不合法时，退到纯默认值（永远不在这里抛错）。"""
    try:
        return model.model_validate(payload)
    except Exception:  # noqa: BLE001
        return model(source=source)


def merge_preserve(preserve: list[str] | None, must_not_change: list[str] | None) -> list[str]:
    """`preserve ⊇ must_not_change`：身份锁定字段一个都不许在组装时丢掉。"""
    out: list[str] = []
    for item in list(preserve or []) + list(must_not_change or []):
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def attrs_of(value: Any) -> ClothingAttrs:
    """把 dict / 模型 / None 统一成 `ClothingAttrs`（节点之间传 dict 更省心）。"""
    if isinstance(value, ClothingAttrs):
        return value
    if isinstance(value, BaseModel):
        return ClothingAttrs.model_validate(value.model_dump())
    return ClothingAttrs.model_validate(value or {})


__all__ = ["ClothingAttrs", "ImageIntent", "DEFAULT_MUST_NOT_CHANGE", "parse_attrs",
           "parse_intent", "merge_preserve", "attrs_of", "HEX_PATTERN"]
