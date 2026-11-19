"""校验：请求级 + Prompt 级规则。

分两类，别混：
- **用户参数错** → 直接抛错（4xx），例如参考图超 6 张、比例不支持、张数越界；
- **链路降级** → 只记 warning（`state.warnings` / `state.mcp_degraded`），例如中台参数没取到、
  模型输出不合 schema。降级要继续出图，不能因为"参数来源不完美"就整单失败。
"""

from __future__ import annotations

from app.agents_image.schemas import FORBIDDEN_WORDS, attrs_of
from app.agents_image.state import ImageTaskState
from app.agents_image.storage.image_storage import get_storage
from app.core.errors import raise_image

ALLOWED_ASPECTS = {"1:1", "3:4", "4:3", "16:9"}
MAX_FEATURE_COUNT = 8


def _texts(state: ImageTaskState) -> list[str]:
    out = [state.prompt or "", state.prompt_final or ""]
    out.extend(draft.prompt for draft in state.prompt_pack)
    out.extend(state.reflections or [])
    return out


def validate(state: ImageTaskState, *, settings=None,
             extra_forbidden: tuple[str, ...] = ()) -> None:
    """全量校验；通过返回 None，不通过抛 ImageError（带错误码）。"""
    max_refs = (settings.max_refs if settings else 6)
    max_total = (settings.max_total_images if settings else 9)
    refs = len(state.refs or [])
    if refs == 0:
        raise_image("IMG_REFERENCE_REQUIRED",
                    "所选商品/素材/相似款没有可用参考图（需要 1–6 张：中台商品图、"
                    "素材库勾选或拍立淘相似款）")
    if refs > max_refs:
        raise_image("IMG_TOO_MANY_REFS", f"参考图最多 {max_refs} 张，收到 {refs} 张")
    if state.aspect not in ALLOWED_ASPECTS:
        raise_image("IMG_INPUT_INVALID", f"aspect 只支持 {sorted(ALLOWED_ASPECTS)}")
    count = int(state.count or 0)
    if not 1 <= count <= min(max_total, 9):
        raise_image("IMG_INPUT_INVALID", f"张数需在 1..{min(max_total, 9)}，收到 {state.count}")
    if not (state.prompt or "").strip():
        raise_image("IMG_INPUT_INVALID", "需求描述不能为空")

    _validate_attrs(state)
    _validate_prompt_pack(state)
    _validate_words(state, extra_forbidden)


def _validate_attrs(state: ImageTaskState) -> None:
    attrs = attrs_of(state.attrs)
    if not attrs.must_not_change:
        # schema 层已保证非空；这里是"改坏了"的兜底断言
        raise_image("IMG_SCHEMA_INVALID", "身份锁定字段（must_not_change）不能为空")
    if len(attrs.key_features) > MAX_FEATURE_COUNT:
        raise_image("IMG_SCHEMA_INVALID", "商品特征条目过多")
    state.attrs = attrs.model_dump()


def _validate_prompt_pack(state: ImageTaskState) -> None:
    drafts = list(state.prompt_pack or [])
    if not drafts:
        # 只有"已组装"的阶段才要求非空；请求预校验时还没 compose
        return
    must_not_change = attrs_of(state.attrs).must_not_change
    for draft in drafts:
        if not (draft.prompt or "").strip():
            raise_image("IMG_SCHEMA_INVALID", f"图位 {draft.slot} 的 Prompt 为空")
        missing = [word for word in must_not_change if word not in (draft.preserve or [])]
        if missing:
            raise_image("IMG_SCHEMA_INVALID",
                        f"图位 {draft.slot} 丢失身份锁定字段：{'、'.join(missing)}")
        if draft.aspect != state.aspect:
            raise_image("IMG_SCHEMA_INVALID",
                        f"图位 {draft.slot} 比例（{draft.aspect}）与请求（{state.aspect}）不一致")


def _validate_words(state: ImageTaskState, extra_forbidden: tuple[str, ...]) -> None:
    banned = tuple(FORBIDDEN_WORDS) + tuple(extra_forbidden or ())
    for text in _texts(state):
        for word in banned:
            if word and word in (text or ""):
                raise_image("IMG_CONTENT_BLOCKED", f"命中禁用表述：{word}")


def degrade_note(state: ImageTaskState) -> None:
    """把链路降级写进 warnings（不阻塞出图），由任务/健康接口暴露给前端。"""
    if state.mcp_degraded and not any("IMG_MCP_DEGRADED" in w for w in state.warnings):
        state.warnings.append("IMG_MCP_DEGRADED: 中台参数未取到，已用本地默认值出图")


__all__ = ["validate", "ALLOWED_ASPECTS", "FORBIDDEN_WORDS", "degrade_note", "get_storage"]
