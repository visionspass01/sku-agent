"""图像理解：先启发式打底，拿到模型/视觉输出再用 `schemas.ClothingAttrs` 收口。

D5 形态：**不调模型**（`raw=None` 直接走启发式），但接口已按最终形态留好——
D6 起 raw 由 MCP 数据中台 / 视觉模型提供，解析失败只降级、不失败（warning 里带 IMG_SCHEMA_INVALID）。
"""

from __future__ import annotations

import re

from app.agents_image.schemas import ClothingAttrs, parse_attrs
from app.agents_image.state import ImageTaskState

_COLOR_WORDS = {
    "白": "#FFFFFF", "黑": "#111111", "米": "#C8B7A6", "灰": "#9CA3AF",
    "红": "#C0392B", "蓝": "#2C5D8F", "绿": "#3F7A5E", "牛仔": "#4A6B8A",
}
_TYPE_WORDS = ("开衫", "卫衣", "衬衫", "T恤", "连衣裙", "裤", "外套", "半身裙", "毛衣", "夹克")


def heuristic_attrs(text: str) -> dict:
    """确定性抽取（无模型可用时的兜底，也是评测里的对照基线）。"""
    color_hex = "#FFFFFF"
    for word, value in _COLOR_WORDS.items():
        if word in text:
            color_hex = value
            break
    product_type = next((word for word in _TYPE_WORDS if word in text), "服装商品")
    return {
        "product_type": product_type,
        "shape": "落肩/常规版型（以参考图为准）",
        "color_hex": color_hex,
        "logo_text": "无",
        "materials": "以参考图为准",
        "key_features": ["参考图中的版型与图案"],
        "category": "男装" if "男" in text else ("女装" if "女" in text else "通用"),
        "must_not_change": ["颜色", "版型", "图案", "logo 文字", "关键部件位置"],
        "source": "heuristic",
        "confidence": 0.2,
    }


def understand(state: ImageTaskState, *, raw: dict | str | None = None) -> dict:
    """产出身份锁定字段（dict）；降级原因写进 `state.warnings`。"""
    text = _normalize(f"{state.prompt} {' '.join(state.scene_styles)} "
                      f"{' '.join(state.compositions)}")
    fallback = heuristic_attrs(text)
    if raw is None:
        return ClothingAttrs.model_validate(fallback).model_dump()
    attrs, warnings = parse_attrs(raw, fallback=fallback)
    if warnings:
        state.warnings.extend(warnings)
    if attrs.source == "heuristic":
        state.mcp_degraded = False   # 本来就是启发式，不算中台降级
    return attrs.model_dump()


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


__all__ = ["understand", "heuristic_attrs"]
