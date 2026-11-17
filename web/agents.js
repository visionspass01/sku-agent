/* ============================================================
 * Agent 目录页脚本（`/agents.html`）
 *
 * 结构约定（这就是「方便后期增加 Agent」的扩展点）：
 *   一个 Agent 演示页 = 一条 `AGENT_PAGES` 数据 + 一个 `web/<page>.html`。
 *   目录页只负责「列出入口 + 展示运行状态」，不加载 app.js、不涉及登录态。
 *
 * 扩展方式（页面不再渲染这段文字，只保留在注释里）：
 *   新增一个 Agent 演示页 =
 *     ① 新增 web/<page>.html（复用同一套 header / 卡片样式）
 *     ② 在 web/agents.js 的 AGENT_PAGES 里加一条数据。
 *   目录页会自动出现卡片，不需要改后端路由
 *   （新页面若需服务端注入 ?v= 版本号，再加一条 render_page_html 路由）。
 *
 * 为什么用严格 JSON 数组而不是 JS 表达式：`tests/test_ui_pages.py` 直接
 * `json.loads` 这段数据做契约校验（每条必须有 id/name/page，page 指向的 HTML 必须存在），
 * 数据里混注释或尾逗号会让护栏失效，所以**只放数据、不放代码**。
 * ============================================================ */

const PORTAL_BUILD = "agents-v1";
window.__UI_BUILD = PORTAL_BUILD;

const AGENT_PAGES = [
  {
    "id": "sku",
    "name": "SKU 监管 Agent",
    "page": "/",
    "tagline": "服装电商 SKU 库存监管与处置：自然语言问数 · 单 SKU 研判 · 补货草稿（超额人工确认）· 批量巡检 · SOP 引用溯源",
    "caps": ["工具面 + 证据链 result_id", "部门权限隔离", "人工确认闸门", "RAG 引用溯源", "多轮记忆"],
    "accounts": "admin / worker1（男装） / worker2（女装）"
  }
];

// 后端没起来时的兜底文案（测试会校验这条不是空字符串，防止 catch 被写成静默）
const AGENT_OFFLINE_TEXT =
  "后端未启动或不可达（/api/health 失败）：先在仓库根跑 " +
  "PYTHONPATH=src uvicorn app.main:app --port 8000，再刷新本页。";

const HEALTH_TIMEOUT_MS = 4000;

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function setBadgeTone(id, tone) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove("badge-ok", "badge-warn");
  if (tone) el.classList.add(tone);
}

// 存储徽标：关系库 + 向量库（与首页 app.js 的 storageLabel/storageTitle 保持一致）
function storageLabel(h) {
  const db = (h && h.database) || "-";
  const backend = h && h.vector_store ? String(h.vector_store.backend || "") : "";
  return backend ? `${db}、${backend}` : db;
}

function storageTitle(h) {
  const db = (h && h.database) || "-";
  const vs = (h && h.vector_store) || {};
  const lines = [`关系库：${db}（业务数据与知识库正文的权威源）`];
  if (vs.backend) {
    const parts = [`向量库：${vs.backend}`];
    if (vs.collection) parts.push(`集合 ${vs.collection}`);
    if (vs.dim) parts.push(`${vs.dim} 维`);
    if (vs.points !== undefined && vs.points !== null) parts.push(`${vs.points} 点`);
    lines.push(parts.join(" · "));
    if (vs.degraded) {
      lines.push(`向量检索已降级：${vs.degraded_reason || "运行期故障"}（退回进程内哈希余弦）`);
    }
  } else {
    lines.push("向量库：服务端未上报（旧版接口）");
  }
  return lines.join("\n");
}

function cardHtml(agent) {
  const caps = (agent.caps || []).map((c) => `<span class="tag">${esc(c)}</span>`).join("");
  const href = esc(agent.page || "/");
  return [
    '<div class="agent-card" data-agent="' + esc(agent.id) + '">',
    '  <div class="agent-card-head">',
    '    <span class="agent-status" data-role="status"><i></i><b>检测中…</b></span>',
    '    <h2>' + esc(agent.name) + '</h2>',
    '  </div>',
    '  <p class="agent-tagline">' + esc(agent.tagline) + '</p>',
    '  <div class="agent-caps">' + caps + '</div>',
    agent.accounts
      ? '  <p class="agent-accounts muted">演示账号：<b>' + esc(agent.accounts) + '</b></p>'
      : "",
    '  <div class="agent-actions">',
    '    <a class="agent-enter" href="' + href + '">进入演示 →</a>',
    '    <a class="mini-btn secondary" href="/about.html" target="_blank" rel="noopener">项目简介</a>',
    '  </div>',
    '</div>'
  ].filter(Boolean).join("\n");
}

function renderAgentCards() {
  const grid = document.getElementById("agent-grid");
  if (!grid) return;
  grid.innerHTML = AGENT_PAGES.map(cardHtml).join("\n");
  setText("agent-count", String(AGENT_PAGES.length));
}

function markCards(online) {
  document.querySelectorAll(".agent-status").forEach((el) => {
    el.classList.toggle("off", !online);
    const label = el.querySelector("b");
    if (label) label.textContent = online ? "在线" : "离线";
  });
}

function showOfflineNote(show) {
  let box = document.getElementById("agent-offline");
  if (!show) {
    if (box) box.remove();
    return;
  }
  if (box) return;
  box = document.createElement("div");
  box.id = "agent-offline";
  box.className = "agent-offline muted";
  box.textContent = "⚠ " + AGENT_OFFLINE_TEXT;
  const grid = document.getElementById("agent-grid");
  if (grid && grid.parentNode) grid.parentNode.insertBefore(box, grid);
}

async function loadHealth() {
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), HEALTH_TIMEOUT_MS) : null;
  try {
    const resp = await fetch("/api/health", controller ? { signal: controller.signal } : {});
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const h = await resp.json();
    setText("badge-service-value", h.status === "ok" ? "正常" : String(h.status || "-"));
    setBadgeTone("badge-service", h.status === "ok" ? "badge-ok" : "badge-warn");
    setText("badge-db", storageLabel(h));
    const dbEl = document.getElementById("badge-db");
    if (dbEl) dbEl.title = storageTitle(h);
    setText("badge-cache", h.cache || "-");
    setText("badge-llm", h.llm_model || "-");
    setText("badge-mcp", h.mcp_transport || "-");
    showOfflineNote(false);
    markCards(true);
  } catch (err) {
    setText("badge-service-value", "离线");
    setBadgeTone("badge-service", "badge-warn");
    showOfflineNote(true);
    markCards(false);
    console.warn("[agents] /api/health 失败：", err && err.message ? err.message : err);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// 与 index.html 的兜底提示同源：页面与脚本版本不一致时给可见横幅（防止浏览器缓存旧脚本）
function checkPortalBuild() {
  const pageBuild = (document.body && document.body.dataset.uiBuild) || "";
  if (!pageBuild || pageBuild === PORTAL_BUILD) return;
  if (document.querySelector(".stale-banner")) return;
  const bar = document.createElement("div");
  bar.className = "stale-banner";
  bar.innerHTML = "⚠ 目录页脚本与页面版本不一致（页面 " + esc(pageBuild) +
    " / 脚本 " + esc(PORTAL_BUILD) + "），请强制刷新（Ctrl+F5）。";
  if (document.body) document.body.insertBefore(bar, document.body.firstChild);
}

function init() {
  renderAgentCards();
  checkPortalBuild();
  loadHealth();
  setInterval(loadHealth, 15000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
