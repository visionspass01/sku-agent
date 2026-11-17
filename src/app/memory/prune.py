"""通用剪枝兜底（等价 dsh-compaction-tool-result-pruner 的三档阈值）。

工具级契约（src/app/memory/result_store.py）已经做了「超过 N 行只回 summary」，
本模块只在把历史工具结果回放进 prompt 时做字符级兜底：4096 / 2048 / 512 三档。
"""

from __future__ import annotations

from app.config import get_settings

DEFAULT_LEVELS = (4096, 2048, 512)


def prune_tool_text(text: str, levels: tuple[int, ...] | list[int] | None = None) -> str:
    """按上下文预算做分级截断：取第一个能放下的档位，都放不下就用最小档。"""
    levels = tuple(levels or get_settings().memory_prune_levels) or DEFAULT_LEVELS
    text = text or ""
    for level in levels:
        if len(text) <= level:
            return text
    smallest = min(levels)
    keep = max(0, smallest - 30)
    return text[:keep] + f"\n…［工具结果已按 {smallest} 字剪枝，完整明细见 result_id/artifact_path］"


def estimate_tokens(text: str) -> int:
    """粗估 token（中文≈1 字 1 token，英文≈4 字 1 token），只用于触发阈值判断。"""
    text = text or ""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + max(1, other // 4)
