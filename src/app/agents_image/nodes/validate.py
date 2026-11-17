"""校验：请求级与 Prompt 级规则（D2 先做基础项，D5 补身份锁定/禁用词等）。"""

from __future__ import annotations

from app.agents_image.state import ImageTaskState
from app.agents_image.storage.image_storage import get_storage
from app.core.errors import raise_image

ALLOWED_ASPECTS = {"1:1", "3:4", "4:3", "16:9"}
FORBIDDEN_WORDS = ("伪造", "官方认证", "真人明星", "冒充实拍")


def validate(state: ImageTaskState, *, settings=None) -> None:
    max_refs = (settings.max_refs if settings else 6)
    max_total = (settings.max_total_images if settings else 9)
    refs = len(state.refs or [])
    if refs == 0:
        raise_image("IMG_REFERENCE_REQUIRED", "图生图必须上传参考图（1–6 张）")
    if refs > max_refs:
        raise_image("IMG_TOO_MANY_REFS", f"参考图最多 {max_refs} 张，收到 {refs} 张")
    if state.aspect not in ALLOWED_ASPECTS:
        raise_image("IMG_INPUT_INVALID", f"aspect 只支持 {sorted(ALLOWED_ASPECTS)}")
    if not 1 <= int(state.count or 0) <= min(max_total, 9):
        raise_image("IMG_INPUT_INVALID", f"张数需在 1..{min(max_total, 9)}，收到 {state.count}")
    if not (state.prompt or "").strip():
        raise_image("IMG_INPUT_INVALID", "需求描述不能为空")
    for text in [d.prompt for d in state.prompt_pack] + [state.prompt]:
        for word in FORBIDDEN_WORDS:
            if word in (text or ""):
                raise_image("IMG_CONTENT_BLOCKED", f"命中禁用表述：{word}")


__all__ = ["validate", "ALLOWED_ASPECTS", "FORBIDDEN_WORDS", "get_storage"]
