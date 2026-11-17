"""前端「一页一 Agent」并行结构的回归测试。

结构约定（`web/agents.js` 顶部注释同款）：
- `web/index.html` = SKU 监管 Agent 演示页（`/`）；
- `web/agents.html` = Agent 目录页（`/agents.html`），只列入口 + `/api/health` 状态，不挂登录；
- 新增 Agent 演示 = `AGENT_PAGES` 加一条数据 + 一个 `web/<page>.html`，不需要改后端逻辑。

这几条测试就是「以后加 Agent 别忘了加页面 / 忘了给目录页注入脚本版本」的护栏。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def _read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def _agent_pages() -> list[dict]:
    """从 `agents.js` 里取出 `AGENT_PAGES`（严格 JSON 数组，只放数据不放代码）。"""
    js = _read("agents.js")
    marker = "const AGENT_PAGES = "
    start = js.index(marker) + len(marker)
    # 数据块结尾是 "\n];"：切到 "]"（含）为止，丢掉语句分号
    end = js.index("\n];", start) + 2
    data = json.loads(js[start:end])
    assert isinstance(data, list)
    return data


# ---------------------------------------------------------------- 目录页路由与资源版本
def test_agents_directory_route_is_versioned(app_client):
    resp = app_client.get("/agents.html")
    assert resp.status_code == 200
    html = resp.text
    # 走 render_page_html：脚本/样式都带 ?v=，避免"页面新、脚本旧"
    assert "/agents.js?v=" in html
    assert "/style.css?v=" in html
    # 目录页自己不加载看板脚本（否则会去查 #chat-messages 之类的元素）
    assert "/app.js" not in html
    assert 'id="agent-grid"' in html
    # CSP 仍是 script-src 'self'：目录页只能有一个外链脚本，不能有内联脚本
    assert html.count("<script") == 1
    assert "<script>" not in html


def test_dashboard_links_to_agents_directory():
    html = _read("index.html")
    assert 'href="/agents.html"' in html, "看板右上角要有「Agent 目录」入口"
    assert 'data-agent="sku"' in html
    # 版本自愈契约不变：页面 build tag 必须等于 app.js 的 UI_BUILD
    js = _read("app.js")
    html_build = html.split('data-ui-build="')[1].split('"')[0]
    js_build = js.split('const UI_BUILD = "')[1].split('"')[0]
    assert html_build == js_build


# ---------------------------------------------------------------- 扩展契约
def test_agent_pages_contract_points_to_real_pages():
    pages = _agent_pages()
    assert pages, "至少要有一个 Agent 演示页（当前的 SKU 监管 Agent）"
    for item in pages:
        for key in ("id", "name", "page"):
            assert item.get(key), f"Agent 条目缺字段 {key}：{item}"
        page = item["page"]
        target = WEB / ("index.html" if page == "/" else page.lstrip("/"))
        assert target.is_file(), f"AGENT_PAGES 里的 page={page!r} 没有对应文件：{target}"
    assert len({item["id"] for item in pages}) == len(pages), "Agent id 不能重复"


def test_agents_directory_has_offline_fallback():
    """后端没起来时目录页要给出可读提示，而不是静默失败。"""
    js = _read("agents.js")
    assert "AGENT_OFFLINE_TEXT" in js
    offline_text = js.split("const AGENT_OFFLINE_TEXT =")[1].split(";")[0]
    assert "/api/health" in offline_text, "离线文案要说明是 /api/health 失败"
    assert "uvicorn" in offline_text, "离线文案要给出一条能照做的启动命令"
    # catch 分支必须调用可见提示，不能只 console
    assert "showOfflineNote(true)" in js
    assert "/api/health" in js


# ---------------------------------------------------------------- 扩展说明只留在注释里
def test_extension_note_is_comment_only():
    """「扩展方式」这段是给维护者看的，留在代码注释里，不在页面上渲染。"""
    html = _read("agents.html")
    assert "agent-note" not in html, "页面上不应再有扩展说明区块"
    assert "扩展方式" in html, "说明本身要保留（放在 HTML 注释里）"
    comment_start = html.find("<!--")
    assert 0 <= comment_start < html.find("扩展方式"), "说明必须位于注释块内"
    # 渲染区（body 里可见的内容）不能再出现这段文字
    body = html[html.index("<body"):]
    assert "扩展方式" not in body
    assert "AGENT_PAGES 里加一条数据" in _read("agents.js"), "扩展约定也要留在脚本注释里"
    assert "render_page_html" in _read("agents.js")


# ---------------------------------------------------------------- 接口文档页（自托管）
def test_agents_directory_links_to_self_hosted_api_docs():
    html = _read("agents.html")
    assert 'href="/api.html"' in html, "目录页的接口文档入口要指向自托管页"
    assert 'href="/docs"' not in html, "FastAPI 自带 /docs 在 CSP 下会白屏，不能作为导航入口"


def test_api_docs_page_uses_same_origin_openapi():
    html = _read("api.html")
    js = _read("api.js")
    assert "/openapi.json" in js, "页面要直接读同源 openapi.json"
    assert "/api.js" in html and "/style.css" in html
    assert 'id="api-body"' in html
    # 不允许任何外部资源引用（CSP default-src 'self' 会拦掉；正文里提到 CDN 只是解释原因）
    for name, text in (("api.html", html), ("api.js", js)):
        assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', text), f"{name} 引用了外链资源"
        assert "@import" not in text, f"{name} 不应 @import 外部样式"
    assert "window.ApiDocs" in js


def test_api_docs_route_served(app_client):
    """静态页由根路径挂载提供，且不被 /api/* 路由吃掉。"""
    resp = app_client.get("/api.html")
    assert resp.status_code == 200
    assert "接口文档" in resp.text
    assert app_client.get("/api.js").status_code == 200
    spec = app_client.get("/openapi.json").json()
    assert isinstance(spec.get("paths"), dict) and spec["paths"]


def test_api_docs_grouping_and_filter():
    """node 侧直接调用 api.js 的纯函数：分组、参数/请求体/响应提取、关键词过滤。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过 api.js 渲染函数检查")

    script = r"""
const fs = require("fs"), vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const sandbox = { console };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const api = sandbox.window.ApiDocs;

const chatOp = {
  summary: "对话流式",
  requestBody: {
    content: {
      "application/json": {
        schema: {
          type: "object",
          required: ["message"],
          properties: {
            message: { type: "string", description: "用户提问" },
            topk: { type: "integer" }
          }
        }
      }
    }
  },
  responses: { "200": { description: "SSE" } }
};
const healthOp = { summary: "健康检查", responses: { "200": { description: "ok" } } };
const ragOp = {
  summary: "知识库检索",
  parameters: [{ name: "q", in: "query", required: true, schema: { type: "string" } }]
};
const spec = {
  paths: {
    "/api/chat/stream": { post: chatOp },
    "/api/health": { get: healthOp },
    "/api/rag/search": { get: ragOp }
  }
};

const groups = api.groupEndpoints(spec);
const names = groups.map((g) => g.name);
const chat = groups.find((g) => g.name === "对话与运行详情").operations[0];
const filtered = api.filterGroups(groups, "rag");
const empty = api.filterGroups(groups, "no-such-endpoint");
console.log(JSON.stringify({
  names,
  chatMethod: chat.method,
  chatPath: chat.path,
  body: chat.bodyFields.map((f) => [f.name, f.type, f.required]),
  filteredNames: filtered.map((g) => g.name),
  emptyHtml: api.renderGroups(empty),
  html: api.renderGroups(filtered)
}));
"""
    with tempfile.TemporaryDirectory() as tmp:
        js = Path(tmp) / "check_api_docs.js"
        js.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [node, str(js), str(WEB / "api.js")],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
    assert proc.returncode == 0, proc.stderr[-1500:]
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["names"] == ["对话与运行详情", "健康检查", "知识库检索"], data["names"]
    assert data["chatMethod"] == "POST" and data["chatPath"] == "/api/chat/stream"
    assert data["body"] == [["message", "string", True], ["topk", "integer", False]], data["body"]
    assert data["filteredNames"] == ["知识库检索"], data["filteredNames"]
    assert "/api/rag/search" in data["html"]
