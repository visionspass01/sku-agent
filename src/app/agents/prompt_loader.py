"""Prompt 文件加载与版本化（提示词外置，便于 review / 调整 / 追溯）。

为什么要把提示词从代码里搬出来：
- 原先对话系统提示是 200 行 f-string，没法 review、没法 diff、没法 A/B；
- 出了问题要能回答「这条结论是哪一版提示词产出的」，所以每次渲染都要带 `prompt_version`。

约定：
- 提示词放在 `src/app/agents/prompts/*.md`，用 `{placeholder}` 占位；
- 只替换「已知键」，其余花括号原样保留（Markdown/JSON 片段不会被 `.format()` 打错）；
- `prompt_version(name)` = **归一化文本**的 sha1 前 8 位，按 mtime 缓存。
  归一化（CRLF/CR → LF）是必须的：`.gitattributes` 是 `eol=lf`，同一份内容在
  Windows 工作区（CRLF）与 Linux CI（LF）上必须给出**同一个**版本号，否则
  「这条结论是哪一版提示词产出的」跨平台对不上（P2-3）。
"""

from __future__ import annotations

import hashlib
import pathlib
import re
from typing import Mapping

PROMPT_DIR = pathlib.Path(__file__).resolve().parent / "prompts"

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_version_cache: dict[str, tuple[float, str]] = {}
_text_cache: dict[str, tuple[float, str]] = {}


def prompt_path(name: str) -> pathlib.Path:
    return PROMPT_DIR / f"{name}.md"


def _normalize(text: str) -> str:
    """换行统一成 LF：版本号与渲染结果都不应该受编辑器/平台换行影响。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_text(path: pathlib.Path) -> str:
    return _normalize(path.read_text(encoding="utf-8"))


def load_prompt(name: str) -> str:
    """读取原始提示词（不做占位替换）。按 mtime 缓存文本：热加载生效，又不至于每轮读盘。"""
    path = prompt_path(name)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _text_cache.pop(name, None)
        raise
    cached = _text_cache.get(name)
    if cached is None or cached[0] != mtime:
        text = _read_text(path)
        _text_cache[name] = (mtime, text)
        return text
    return cached[1]


def prompt_version(name: str) -> str:
    """提示词版本号（**归一化文本**的 sha1 前 8 位，按 mtime 缓存）。"""
    path = prompt_path(name)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return "missing"
    cached = _version_cache.get(name)
    if cached is None or cached[0] != mtime:
        digest = hashlib.sha1(_read_text(path).encode("utf-8")).hexdigest()[:8]
        _version_cache[name] = (mtime, digest)
        return digest
    return cached[1]


def render_text(template: str, values: Mapping[str, object]) -> str:
    """渲染占位符：只替换传入的键，未传入的 `{xxx}` 原样保留。"""

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        return str(values[key])

    return _PLACEHOLDER.sub(_sub, template)


def render_prompt(name: str, **values: object) -> str:
    return render_text(load_prompt(name), values)


def reload_prompts() -> None:
    """清掉版本/文本缓存，让下一次 load/render 读到最新文件内容（配合热加载）。"""
    _version_cache.clear()
    _text_cache.clear()


def prompt_versions() -> dict[str, str]:
    """当前所有提示词的版本（/api/health 暴露，便于确认线上跑的是哪一版）。"""
    names = sorted(p.stem for p in PROMPT_DIR.glob("*.md"))
    return {name: prompt_version(name) for name in names}
