"""生图 Agent 注册表：manifest + 提示词热加载（数据即配置，改文件即生效）。"""

from __future__ import annotations

import json
import logging
import pathlib

from app.core import prompts as prompt_lib
from app.core.hotreload import HotReloader

logger = logging.getLogger("sku-agent.image.registry")

BASE = pathlib.Path(__file__).resolve().parent
MANIFEST_DIR = BASE / "manifests"
PROMPT_DIR = BASE / "prompts"

DEFAULT_MANIFEST = {
    "id": "image",
    "name": "电商生图 Agent",
    "prompt": "image_system",
    "generator_prompt": "prompt_generator",
    "platform": "淘系",
    "max_refs": 6,
    "max_total_images": 9,
}


class ImageRegistry:
    def __init__(self) -> None:
        self.manifest: dict = dict(DEFAULT_MANIFEST)
        self.errors: list[str] = []
        self._manifest_loader = HotReloader(MANIFEST_DIR, "*.json")
        self._prompt_loader = HotReloader(PROMPT_DIR, "*.md")
        self.load()

    def load(self) -> None:
        errors: list[str] = []
        path = MANIFEST_DIR / "image.json"
        try:
            self.manifest = {**DEFAULT_MANIFEST, **json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, ValueError) as exc:
            errors.append(f"manifest 加载失败：{exc}")
        self.errors = errors
        self._manifest_loader.mark()
        self._prompt_loader.mark()

    def maybe_reload(self) -> bool:
        changed = False
        if self._manifest_loader.changed() or self._prompt_loader.changed():
            self.load()
            changed = True
        return changed

    def prompt(self, name: str, values: dict | None = None) -> tuple[str, str]:
        return prompt_lib.load_prompt(name, directory=PROMPT_DIR, values=values)

    def health(self) -> dict:
        return {"manifest": self.manifest.get("id"), "errors": list(self.errors),
                "prompts": sorted(p.stem for p in PROMPT_DIR.glob("*.md"))}


_REGISTRY: ImageRegistry | None = None


def get_registry() -> ImageRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = ImageRegistry()
    return _REGISTRY


def reset_registry() -> None:
    global _REGISTRY
    _REGISTRY = None
    prompt_lib.clear_cache()
