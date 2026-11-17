"""图位规划与 Prompt 组装（D2 先做「单/多张同图位」的最小实现，D8 再扩成 9 图位）。"""

from __future__ import annotations

from app.agents_image.state import ImageTaskState, PromptDraft


def build_slots(state: ImageTaskState) -> list[dict]:
    """按请求张数生成图位（D8 起改为中台 plan_policy 驱动的 9 图位）。"""
    count = max(1, min(int(state.count or 1), 9))
    roles = {1: "主图", 2: "上装卖点", 3: "下装卖点", 4: "场景 A",
             5: "场景 B", 6: "细节特写", 7: "尺码对照", 8: "包装清单", 9: "售后说明"}
    return [{"slot": i, "role": roles.get(i, f"图位 {i}"), "type": "generate"}
            for i in range(1, count + 1)]


def compose(state: ImageTaskState, registry) -> list[PromptDraft]:
    """把「身份锁定 + 图位 + 场景风格」套进五槽位模板，产出每图位的 PromptDraft。"""
    attrs = state.attrs or {}
    must_not_change = list(attrs.get("must_not_change") or
                           ["颜色", "版型", "图案", "logo 文字"])
    product_block = (
        f"{attrs.get('product_type', '服装商品')}；形状/版型：{attrs.get('shape', '以参考图为准')}；"
        f"主色 {attrs.get('color_hex', '#FFFFFF')}；材质：{attrs.get('materials', '以参考图为准')}；"
        f"logo/文字：{attrs.get('logo_text', '无')}"
    )
    scene_style = (state.scene_styles[0] if state.scene_styles else
                   registry.manifest.get("default_background", "白底电商风"))
    composition = state.compositions[0] if state.compositions else "正面全身像·上装突出"
    background = "纯白" if "白底" in scene_style else "与场景风格一致"
    width, height = _aspect_size(state.aspect)

    drafts: list[PromptDraft] = []
    for item in state.slots or build_slots(state):
        slot_no = item.get("slot") if isinstance(item, dict) else getattr(item, "slot", 1)
        role = item.get("role", "主图") if isinstance(item, dict) else getattr(item, "role", "主图")
        text, _version = registry.prompt(registry.manifest["generator_prompt"], {
            "product_block": product_block,
            "scene_block": f"{composition}；场景风格：{scene_style}；商品占画面 70%，四周留白 8%",
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
            aspect=state.aspect, width=width, height=height, preserve=must_not_change,
        ))
    return drafts


def _aspect_size(aspect: str) -> tuple[int, int]:
    return {"1:1": (1024, 1024), "3:4": (768, 1024), "4:3": (1024, 768),
            "16:9": (1024, 576)}.get(aspect, (1024, 1024))


__all__ = ["build_slots", "compose"]
