"""静态资源版本化：给页面里的 app.js / agents.js / style.css 追加内容哈希，根治"HTML 新、JS 旧"。

思路：
- `asset_versions()` 按文件内容算 sha1 前 8 位（按 mtime 缓存，改文件即失效）；
- `render_page_html(name)` 读取 `web/<name>`，把 `src="/app.js"` / `href="/style.css"` 改写成带 `?v=<hash>`；
- 浏览器每次拿到的 HTML 与脚本版本天然一致；脚本内容一变 URL 就变，缓存不会命中旧文件。

多页面说明：前端是「一个页面一个 Agent」的并行结构（`index.html` = SKU 监管 Agent 演示页，
`agents.html` = Agent 目录页），两台页都要走同一条渲染路径，否则新页面会拿到不带版本号的
旧脚本。新增页面时：在 `main.py` 注册一条 `render_page_html("<页面>.html")` 路由即可。
"""

from __future__ import annotations

import hashlib
import pathlib

from app.config import STATIC_DIR

# 所有页面共用的静态资源：任一文件改了，所有页面的 ?v= 一起变
_ASSETS = ("app.js", "agents.js", "style.css")
_cache: dict[str, tuple[float, str]] = {}


def _digest(path: pathlib.Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return "missing"
    return hashlib.sha1(data).hexdigest()[:8]


def asset_versions() -> dict[str, str]:
    """{'app.js': 'ab12cd34', 'style.css': 'ef56gh78'}（按 mtime 缓存）。"""
    out: dict[str, str] = {}
    for name in _ASSETS:
        path = pathlib.Path(STATIC_DIR) / name
        try:
            mtime = path.stat().st_mtime
        except OSError:
            out[name] = "missing"
            continue
        cached = _cache.get(name)
        if cached is None or cached[0] != mtime:
            cached = (mtime, _digest(path))
            _cache[name] = cached
        out[name] = cached[1]
    return out


def render_page_html(name: str = "index.html") -> str:
    """读 `web/<name>` 并注入资源版本（失败时退回原样，不影响页面可用性）。"""
    path = pathlib.Path(STATIC_DIR) / name
    html = path.read_text(encoding="utf-8")
    versions = asset_versions()
    for asset, version in versions.items():
        if version == "missing":
            continue
        base = f"/{asset}"
        html = html.replace(f'src="{base}"', f'src="{base}?v={version}"')
        html = html.replace(f'href="{base}"', f'href="{base}?v={version}"')
    return html


def render_index_html() -> str:
    """SKU 监管 Agent 演示页（`/`）—— `render_page_html` 的别名，保留历史调用点。"""
    return render_page_html("index.html")


def app_features() -> list[str]:
    """后端能力自述：前端自检面板与运维排查都用它判断"后端是否支持某字段"。"""
    return [
        "run_detail",
        "run_timeline",
        "llm_events",
        "working_set_underbar",
        "query_plan",
        "scheduler_status",
        "chat_stream",
        "token_stream",
        "grounding",
        "after_sales_approval",
        "mcp_pool_breaker",
        "latency",
        "tool_stats",
        "cost",
        "vector_store",
        "embedding",
        "qdrant",
        "checkpoint_persistence",
        "history_message_count",
        "asset_versioning",
    ]
