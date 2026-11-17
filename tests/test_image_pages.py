"""D14–D15 前端契约：`/image.html` 可访问、脚本版本化、入口卡片、无上传入口、SVG 手绘图。"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def _read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def test_image_page_is_served_with_versioned_assets(app_client):
    resp = app_client.get("/image.html")
    assert resp.status_code == 200
    html = resp.text
    assert "/image.js?v=" in html
    assert "/image.css?v=" in html
    assert "/image_admin.js?v=" in html
    assert "/style.css?v=" in html
    # 一页一 Agent：本页不加载看板脚本
    assert "/app.js" not in html
    # CSP 只允许外链脚本，不允许内联
    assert html.count("<script") == 2
    assert "<script>" not in html


def test_image_page_has_required_anchors():
    html = _read("image.html")
    for element_id in ("login-panel", "workbench", "admin-panel", "product-rows",
                       "composition-select", "scene-style-select", "count-input",
                       "aspect-select", "prompt-input", "btn-create", "progress",
                       "gallery", "metric-cards", "image-chart"):
        assert f'id="{element_id}"' in html, element_id
    # 没有上传入口（v6：参考图只能来自中台商品图）
    assert 'type="file"' not in html
    assert "upload" not in html.lower()


def test_image_js_uses_auth_headers_and_fetch_stream():
    js = _read("image.js")
    # SSE 走 fetch + Authorization 头，而不是把 token 放 URL
    assert "openStream" in js
    assert "new EventSource" not in js          # 注释里可以解释"为什么不用 EventSource"
    assert "Authorization" in js or "authHeaders" in js
    assert "token=" not in js
    # 关键接口都在用
    for path in ("/api/image/auth/login", "/api/image/products", "/api/image/catalog",
                 "/api/image/tasks", "/stream", "/api/image/quota"):
        assert path in js, path
    admin_js = _read("image_admin.js")
    for path in ("/api/image/admin/metrics", "/api/image/admin/tasks", "/api/image/admin/quota"):
        assert path in admin_js, path
    # 独立登录态
    assert "image_agent_token" in js
    assert "image-v1" in js


def test_image_js_has_no_legacy_upload_path():
    js = _read("image.js")
    assert "/api/image/upload" not in js
    assert "ref_asset_ids" not in js


def test_admin_panel_renders_svg_chart_without_libraries():
    js = _read("image_admin.js")
    assert "createElementNS" in js
    assert "http://www.w3.org/2000/svg" in js
    assert "polyline" in js
    assert "by_user" in js                      # 悬停账户细分
    # 零外部依赖：不能出现 CDN 脚本或第三方前端库
    assert "https://" not in js and "http://" not in js.replace("http://www.w3.org/2000/svg", "")
    for label in ("缓存命中率", "LLM", "生成中图片", "累计交付", "首轮通过率"):
        assert label in js, label


def test_agent_pages_contains_image_entry():
    js = _read("agents.js")
    marker = "const AGENT_PAGES = "
    start = js.index(marker) + len(marker)
    end = js.index("\n];", start) + 2
    pages = json.loads(js[start:end])
    ids = [item["id"] for item in pages]
    assert ids == ["sku", "image"]
    image = next(item for item in pages if item["id"] == "image")
    assert image["page"] == "/image.html"
    assert (WEB / "image.html").is_file()
    assert image["caps"] and image["accounts"]


def test_image_css_defines_layout_classes():
    css = _read("image.css")
    for selector in (".image-table", ".image-gallery", ".progress-item",
                     ".image-chart-box", ".image-chart-tip", ".image-admin-grid"):
        assert selector in css, selector


def test_static_asset_versions_include_image_assets():
    from app.static_assets import asset_versions

    versions = asset_versions()
    for name in ("image.js", "image_admin.js", "image.css"):
        assert name in versions
        assert versions[name] != "missing"


def test_image_page_has_no_inline_handlers():
    html = _read("image.html")
    assert not re.search(r'\son(click|change|input)="', html), "禁止内联事件（CSP 兼容）"
