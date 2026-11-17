"""图像理解（D2–D3 先用确定性启发式；D5 接结构化 schema，D6 起参数改由 MCP 中台提供）。"""

from __future__ import annotations

import re

from app.agents_image.state import ImageTaskState

_COLOR_WORDS = {
    "白": "#FFFFFF", "黑": "#111111", "米": "#C8B7A6", "灰": "#9CA3AF",
    "红": "#C0392B", "蓝": "#2C5D8F", "绿": "#3F7A5E", "牛仔": "#4A6B8A",
}
_TYPE_WORDS = ("开衫", "卫衣", "衬衫", "T恤", "连衣裙", "裤", "外套", "半身裙", "毛衣", "夹克")


def understand(state: ImageTaskState) -> dict:
    """从需求文本 + 图位提示里抽身份锁定字段（确定性，不调模型）。"""
    text = f"{state.prompt} {' '.join(state.scene_styles)} {' '.join(state.compositions)}"
    color_hex = "#FFFFFF"
    for word, value in _COLOR_WORDS.items():
        if word in text:
            color_hex = value
            break
    product_type = next((word for word in _TYPE_WORDS if word in text), "服装商品")
    shape = "落肩/常规版型（以参考图为准）"
    return {
        "product_type": product_type,
        "shape": shape,
        "color_hex": color_hex,
        "logo_text": "无",
        "materials": "以参考图为准",
        "key_features": ["参考图中的版型与图案"],
        "category": "男装" if "男" in text else ("女装" if "女" in text else "通用"),
        "must_not_change": ["颜色", "版型", "图案", "logo 文字", "关键部件位置"],
        "source": "heuristic",
    }


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
