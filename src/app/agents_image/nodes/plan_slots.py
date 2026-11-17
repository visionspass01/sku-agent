"""图位规划与 Prompt 组装。

v6 起的边界：
- **图位模板、默认构图、默认场景风格一律来自中台**（`plan_policy` / `list_*`，经 MCP）；
  本模块不再硬编码"正面全身像·上装突出""白底电商风"这类默认值；
- 建任务时就把「图位 + 构图 label + 场景风格 label」写进 `slots_json`，执行期（`graph`）
  不再回调中台 —— 任务保持无状态、可重放、可对比。
"""

from __future__ import annotations

from app.agents_image.schemas import attrs_of, merge_preserve
from app.agents_image.state import ImageTaskState, PromptDraft


def build_slots(state: ImageTaskState, *, policy=None, labels: dict | None = None
                ) -> list[dict]:
    """按中台 `plan_policy` 生成图位（截断到请求张数）；无 policy 时退到通用占位图位。"""
    count = max(1, min(int(state.count or 1), 9))
    labels = labels or {}
    composition_labels = labels.get("compositions") or {}
    style_labels = labels.get("scene_styles") or {}
    specs = list(getattr(policy, "slots", []) or [])
    user_composition = (state.compositions[0] if state.compositions else "")
    user_style = (state.scene_styles[0] if state.scene_styles else "")

    slots: list[dict] = []
    for index in range(1, count + 1):
        spec = next((item for item in specs if int(getattr(item, "slot", 0)) == index), None)
        composition_id = user_composition or getattr(spec, "composition_id", "") or \
            getattr(policy, "default_composition_id", "")
        style_id = user_style or getattr(spec, "scene_style_id", "") or \
            getattr(policy, "default_scene_style_id", "")
        role = getattr(spec, "role", "") or f"图位 {index}"
        slots.append({
            "slot": index,
            "role": role,
            "type": getattr(spec, "type", "") or "generate",
            "composition_id": composition_id,
            "composition_label": composition_labels.get(composition_id, "")
            or (getattr(policy, "default_composition_label", "")
                if composition_id == getattr(policy, "default_composition_id", "") else ""),
            "scene_style_id": style_id,
            "scene_style_label": style_labels.get(style_id, "")
            or (getattr(policy, "default_scene_style_label", "")
                if style_id == getattr(policy, "default_scene_style_id", "") else ""),
        })
    return slots


def compose(state: ImageTaskState, registry) -> list[PromptDraft]:
    """把「身份锁定 + 图位 + 构图/场景（中台给的 label）」套进五槽位模板。"""
    attrs = attrs_of(state.attrs)
    must_not_change = list(attrs.must_not_change)
    product_block = attrs.prompt_block()
    default_background = registry.manifest.get("default_background", "")
    width, height = _aspect_size(state.aspect)

    drafts: list[PromptDraft] = []
    for item in state.slots or build_slots(state):
        slot_no = _field(item, "slot", 1)
        role = _field(item, "role", "主图")
        composition = _field(item, "composition_label", "") or _first(state.compositions)
        scene_style = _field(item, "scene_style_label", "") or _first(state.scene_styles)
        background = _field(item, "background", "") or (
            "纯白" if "白底" in scene_style else default_background or "与场景风格一致")
        text, _version = registry.prompt(registry.manifest["generator_prompt"], {
            "product_block": product_block,
            "scene_block": (f"{composition}；场景风格：{scene_style}；商品占画面 70%，四周留白 8%"
                            if (composition or scene_style) else "商品占画面 70%，四周留白 8%"),
            "light_block": "柔和棚拍光，方向自左上，sRGB，颜色准确",
            "aspect": state.aspect,
            "width": width,
            "height": height,
            "background": background,
            "must_not_change": "、".join(must_not_change),
            "slot": slot_no,
            "role": role,
            "task_prompt": state.prompt,
        })
        drafts.append(PromptDraft(
            slot=int(slot_no or 1), role=role,
            prompt=text, negative_prompt="文字水印、边框、拼图、多余物件、改变款式与颜色",
            aspect=state.aspect, width=width, height=height,
            preserve=merge_preserve(None, must_not_change),
        ))
    return drafts


def _field(item, name: str, default):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _first(values: list[str]) -> str:
    return values[0] if values else ""


def _aspect_size(aspect: str) -> tuple[int, int]:
    return {"1:1": (1024, 1024), "3:4": (768, 1024), "4:3": (1024, 768),
            "16:9": (1024, 576)}.get(aspect, (1024, 1024))


__all__ = ["build_slots", "compose"]
