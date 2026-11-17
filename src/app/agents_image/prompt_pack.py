"""图位 Prompt 输出：把 `PromptDraft` 列表渲染成 `prompt_pack.yaml`（回写中台与人工审计共用）。

YAML 一旦写死在 Prompt 里就不会被复用，所以这里产出的结构是**稳定契约**：
`task_id / batch_id / product_id / slots[] / prompt_pack_yaml`，下游可以按图位重放。
"""

from __future__ import annotations

import yaml

from app.agents_image.state import ImageTaskState, PromptDraft


def pack_document(state: ImageTaskState, drafts: list[PromptDraft]) -> dict:
    """结构化 prompt pack（JSON 友好；写中台 `prompt_pack_json` 与前端展示都用它）。"""
    return {
        "task_id": state.task_id,
        "batch_id": state.batch_id,
        "product_id": state.product_id or "",
        "platform": state.platform,
        "aspect": state.aspect,
        "count": state.count,
        "prompt_version": (state.attrs or {}).get("source", ""),
        "slots": [{
            "slot": draft.slot,
            "role": draft.role,
            "type": draft.type,
            "aspect": draft.aspect,
            "width": draft.width,
            "height": draft.height,
            "fill_percent": draft.fill_percent,
            "margin_percent": draft.margin_percent,
            "text_policy": draft.text_policy,
            "preserve": list(draft.preserve),
            "fragments": list(draft.fragments),
            "prompt": draft.prompt,
            "negative_prompt": draft.negative_prompt,
        } for draft in drafts],
    }


def pack_yaml(state: ImageTaskState, drafts: list[PromptDraft]) -> str:
    """`prompt_pack.yaml` 全文（回写中台时带全文，便于运营复用与审计）。"""
    return yaml.safe_dump(pack_document(state, drafts), allow_unicode=True,
                          sort_keys=False, default_flow_style=False)


__all__ = ["pack_document", "pack_yaml"]
