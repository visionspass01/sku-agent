"""版本化提示词加载：占位符校验 + 内容哈希版本 + mtime 缓存（不依赖 SKU 的 prompt_loader）。

用法：
    text, version = load_prompt("image_system", directory=prompts_dir, must_not_change="颜色/版型")
"""

from __future__ import annotations

import hashlib
import pathlib
import re
from functools import lru_cache

PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class PromptError(RuntimeError):
    """提示词缺失 / 占位符缺参 / 编码异常。"""


@lru_cache(maxsize=64)
def _read_cached(path: pathlib.Path, mtime: float) -> str:
    return path.read_text(encoding="utf-8")


def read_template(name: str, directory: pathlib.Path | str) -> str:
    """读 `<directory>/<name>.md`（按 mtime 缓存）。"""
    base = pathlib.Path(directory)
    path = base / f"{name}.md"
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise PromptError(f"提示词不存在：{path}") from exc
    try:
        return _read_cached(path, mtime)
    except OSError as exc:  # pragma: no cover —— 磁盘异常
        raise PromptError(f"提示词读取失败：{path}（{exc}）") from exc


def render(text: str, values: dict[str, object] | None = None) -> str:
    """只替换「已知占位符」；未提供的占位符直接报错，避免静默渲染出半成品提示词。"""
    values = values or {}

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise PromptError(f"提示词占位符缺少取值：{{{key}}}")
        return str(values[key])

    return PLACEHOLDER_RE.sub(_sub, text)


def prompt_version(text: str) -> str:
    """内容哈希（归一化换行后 sha1 前 8 位）：提示词改一个字，版本就变。"""
    normalized = text.replace("\r\n", "\n").strip()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]


def load_prompt(name: str, *, directory: pathlib.Path | str,
                values: dict[str, object] | None = None) -> tuple[str, str]:
    """渲染提示词并返回 (text, version)。version 用**模板原文**算，便于回答「哪一版产出的」。"""
    template = read_template(name, directory)
    version = prompt_version(template)
    return render(template, values), version


def clear_cache() -> None:
    _read_cached.cache_clear()
