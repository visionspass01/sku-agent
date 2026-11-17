"""L2 工作区记忆：AGENTS.md 基线注入 + Skills 按需加载。

- 基线（AGENTS.md）每轮常驻注入系统提示，内容稳定、体积小（<1.5KB）；
- Skills 只把「名字 + 一句话适用场景」放进系统提示（<300 字），
  模型判断需要时再调用 `load_skill` 拉取全文——避免一上来就塞满上下文。
"""

from __future__ import annotations

from pathlib import Path

from app.config import get_settings

SKILLS_DIR = Path(__file__).resolve().parent.parent / "agents" / "skills"
SKILL_NAMES = (
    "sku-screening",
    "size-gap-diagnosis",
    "overstock-review",
    "restock-calc",
    "weekly-ops-review",
)


def load_workspace_baseline() -> str:
    """读取项目根 AGENTS.md（工作区基线）。缺失/异常降级为空串，不影响对话。"""
    settings = get_settings()
    path = settings.workspace_baseline_file
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        return ""
    limit = settings.memory_context_chars
    return text[:limit]


def _parse_skill(text: str, fallback_name: str) -> tuple[str, str]:
    """从 skill markdown 里取 (name, 一句话描述)：首行标题 + 首个引用块/首句。"""
    name, desc = fallback_name, ""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and name == fallback_name:
            name = stripped.lstrip("# ").strip() or fallback_name
            continue
        if stripped.startswith(">"):
            desc = stripped.lstrip("> ").strip()
            break
        if desc == "" and not stripped.startswith("```"):
            desc = stripped[:60]
            break
    return name, desc


def list_skills() -> list[dict]:
    """列出全部 skill 的元信息（进系统提示用，只含名字与一句话描述）。"""
    items: list[dict] = []
    for name in SKILL_NAMES:
        path = SKILLS_DIR / f"{name}.md"
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        parsed_name, desc = _parse_skill(text, name)
        items.append({"name": name, "title": parsed_name, "summary": desc[:80],
                      "chars": len(text)})
    return items


def skills_catalog_text() -> str:
    items = list_skills()
    if not items:
        return ""
    lines = [f"- {it['name']}：{it['summary']}" for it in items]
    return "## 可用 Skills（需要时调用 load_skill 加载全文）\n" + "\n".join(lines)


def load_skill(name: str) -> dict:
    """按需加载 skill 全文。未知名返回 error（让模型自己纠正，不抛异常）。"""
    raw = (name or "").strip()
    key = raw[:-3] if raw.endswith(".md") else raw
    if key not in SKILL_NAMES:
        return {"error": f"未知 skill：{raw}", "available": list(SKILL_NAMES)}
    path = SKILLS_DIR / f"{key}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"skill 读取失败：{type(exc).__name__}"}
    title, desc = _parse_skill(text, key)
    return {"name": key, "title": title, "summary": desc, "content": text[:6000],
            "chars": len(text)}
