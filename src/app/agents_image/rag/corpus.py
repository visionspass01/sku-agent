"""语料加载：把 `data/kb_image/**/*.md` 解析成带元数据的文档。

front matter 用极简 `key: value`（不引 YAML 依赖，避免因为一个注脚就得装库）：
```
---
id: slot-main-image
kind: slot          # platform | slot | fragment | fix
title: 图位 1 主图
tags: 主图, 白底
---
正文……
```
`kind` 决定检索时的过滤面：平台规则、图位骨架、写法片段、修复经验。
"""

from __future__ import annotations

import hashlib
import pathlib
import re
from dataclasses import dataclass, field

DEFAULT_KB_DIR = pathlib.Path(__file__).resolve().parents[4] / "data" / "kb_image"


@dataclass
class Doc:
    doc_id: str
    kind: str
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    path: str = ""

    @property
    def text(self) -> str:
        return f"{self.title} {self.body} {' '.join(self.tags)}"

    def as_dict(self) -> dict:
        return {"doc_id": self.doc_id, "kind": self.kind, "title": self.title,
                "body": self.body, "tags": list(self.tags), "path": self.path}


def _parse(text: str) -> tuple[dict, str]:
    meta: dict[str, str] = {}
    body = text
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if match:
        for line in match.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip().lower()] = value.strip()
        body = match.group(2)
    return meta, body.strip()


def load_corpus(directory: pathlib.Path | str | None = None) -> list[Doc]:
    """读取目录下所有 `.md`；目录不存在返回空列表（RAG 是增强，不是硬依赖）。"""
    base = pathlib.Path(directory) if directory else DEFAULT_KB_DIR
    if not base.exists():
        return []
    docs: list[Doc] = []
    for path in sorted(base.rglob("*.md")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover —— 磁盘异常
            continue
        meta, body = _parse(raw)
        doc_id = meta.get("id") or path.stem
        docs.append(Doc(
            doc_id=doc_id, kind=(meta.get("kind") or "doc").lower(),
            title=meta.get("title") or path.stem, body=body,
            tags=[tag.strip() for tag in (meta.get("tags") or "").split(",") if tag.strip()],
            path=str(path),
        ))
    return docs


def corpus_fingerprint(docs: list[Doc]) -> str:
    """按文档内容算指纹：语料改了索引自动重建（热插拔）。"""
    digest = hashlib.sha1()
    for doc in docs:
        digest.update(f"{doc.doc_id}:{doc.text}".encode("utf-8"))
    return digest.hexdigest()[:12]


__all__ = ["Doc", "DEFAULT_KB_DIR", "load_corpus", "corpus_fingerprint"]
