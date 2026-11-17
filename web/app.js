/* ============================================================
 * 电商 SKU 监管 Agent 看板脚本
 * 权限：admin（管理员，全部门汇总） / user（部门用户，数据隔离）
 * 登录态：localStorage Token + Authorization: Bearer
 * ============================================================ */

const TOKEN_KEY = "sku_agent_token";

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  user: null,
  metrics: null,
  loginMessage: "",
};

// 与 index.html 的 data-ui-build 对应：不一致说明浏览器缓存了旧脚本
const UI_BUILD = "v20261110-ui9";
window.__UI_BUILD = UI_BUILD;
console.info("[sku-agent] UI build:", UI_BUILD);
// 前端错误留档：自检面板会展示最近几条，便于定位"点了没反应"
const UI_ERRORS = [];
window.addEventListener("error", (e) => {
  UI_ERRORS.push(`${new Date().toLocaleTimeString()} error: ${e.message} @${e.filename || "?"}:${e.lineno || 0}`);
  if (UI_ERRORS.length > 20) UI_ERRORS.shift();
});
window.addEventListener("unhandledrejection", (e) => {
  const msg = (e.reason && (e.reason.message || e.reason)) || "unknown rejection";
  UI_ERRORS.push(`${new Date().toLocaleTimeString()} reject: ${msg}`);
  if (UI_ERRORS.length > 20) UI_ERRORS.shift();
});
// 最近一次对话响应（自检面板展示键名、是否含 run_detail/query_plan）
let LAST_CHAT_DEBUG = null;

const chatState = {
  sessionId: null, sending: false, queryPlan: false,
  streamBubble: null,   // 流式输出中的助手气泡（done 时用最终答复覆盖）
  progressEl: null,     // 进度卡元素（首个 delta 到来时移除）
};
const patrolState = { runId: null, running: false };

const ROLE_LABELS = { admin: "管理员", user: "用户" };
const DEPT_LABELS = { "男装": "男装部门", "女装": "女装部门" };

const $ = (sel) => document.querySelector(sel);

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtTokens(n) {
  const v = Number(n) || 0;
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}m`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1)}k`;
  return `${v}`;
}

function deptLabel(department) {
  return DEPT_LABELS[department] || (department || "全部门");
}

function scopeSuffix(metrics) {
  if (!metrics || !metrics.scope) return "";
  if (metrics.scope.role === "admin") return "（全部门汇总）";
  if (metrics.scope.role === "user") return `（${deptLabel(metrics.scope.department)}）`;
  return "";
}

function riskTag(level) {
  const cls = level === "高" ? "high" : level === "中" ? "mid" : "low";
  return `<span class="tag ${cls}">风险 ${esc(level)}</span>`;
}

// ---------------------------------------------------------------- 请求
async function request(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const resp = await fetch(path, Object.assign({}, opts, { headers }));
  if (resp.status === 401) {
    clearToken();
    renderAccount();
    renderAnonymous();
    resetChatState("登录已失效，请重新登录账号后继续对话。");
    openLogin("登录已失效，请重新登录账号");
    throw new Error("未登录");
  }
  if (!resp.ok) {
    let text = resp.statusText;
    try {
      const body = await resp.json();
      text = body.detail || JSON.stringify(body);
    } catch (e) { /* ignore */ }
    throw new Error(text);
  }
  return resp.json();
}

async function api(path, opts = {}) {
  return request(path, opts);
}

// ---------------------------------------------------------------- 登录态
function setToken(token) {
  state.token = token;
  localStorage.setItem(TOKEN_KEY, token);
}

function clearToken() {
  state.token = "";
  localStorage.removeItem(TOKEN_KEY);
  state.user = null;
  state.metrics = null;
}

function renderAccount() {
  const isLogin = !!state.user;
  $("#btn-login").classList.toggle("hidden", isLogin);
  $("#account-chip").classList.toggle("hidden", !isLogin);
  $("#account-menu").classList.add("hidden");
  if (isLogin) {
    const u = state.user;
    $("#account-avatar").textContent = (u.username || "?").slice(0, 1).toUpperCase();
    const roleText = u.role === "admin" ? "管理员" : deptLabel(u.department);
    $("#account-name").textContent = `${u.username} · ${roleText}`;
    $("#account-head").innerHTML =
      `<b>${esc(u.username)}</b>（${u.role === "admin" ? "管理员" : "部门用户"}）<br/>` +
      `${u.role === "admin" ? "数据范围：全部部门" : `数据范围：${deptLabel(u.department)}`}`;
  } else {
    $("#account-name").textContent = "-";
    $("#account-head").textContent = "";
  }
}

// ---------------------------------------------------------------- 登录弹窗
function openLogin(message) {
  state.loginMessage = message || "";
  $("#login-error").textContent = state.loginMessage;
  $("#login-username").value = state.user ? state.user.username : "";
  $("#login-password").value = "";
  $("#login-modal").classList.remove("hidden");
  setTimeout(() => $("#login-username").focus(), 30);
}

function closeLogin() {
  $("#login-modal").classList.add("hidden");
  $("#login-error").textContent = "";
}

async function submitLogin(event) {
  event.preventDefault();
  const username = $("#login-username").value.trim();
  const password = $("#login-password").value;
  if (!username || !password) {
    $("#login-error").textContent = "请输入用户名和密码";
    return;
  }
  $("#login-submit").disabled = true;
  try {
    const resp = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || resp.statusText || "登录失败");
    setToken(data.token);
    state.user = data.user;
    closeLogin();
    renderAccount();
    resetChatState(`已登录（${data.user.username}），可以开始新的对话。`);
    chatState.sessionId = null;
    await refreshData();
  } catch (err) {
    $("#login-error").textContent = err.message || "登录失败";
  } finally {
    $("#login-submit").disabled = false;
  }
}

async function doLogout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (e) { /* 已失效也允许本地登出 */ }
  clearToken();
  renderAccount();
  renderAnonymous();
  resetChatState("已退出登录，请登录后继续对话。");
  await refreshData();
}

// ---------------------------------------------------------------- 通用弹窗
function openModal(title, bodyHtml) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = bodyHtml;
  $("#modal-overlay").classList.remove("hidden");
}

function closeModal() {
  $("#modal-overlay").classList.add("hidden");
}

async function openSecurityModal() {
  try {
    const data = await api("/api/auth/security-records");
    const rows = (data.records || []).map((r) => `
      <tr><td class="mono">${esc(r.username)}</td><td>${esc(r.ip)}</td><td>${esc(r.at)}</td></tr>
    `).join("") || '<tr><td colspan="3" class="muted">暂无登录记录</td></tr>';
    openModal("安全记录", `
      <div class="security-note">${esc(data.note || "仅保留近 4 小时登录记录")}</div>
      <table class="breakdown-table">
        <thead><tr><th>账号</th><th>登录 IP</th><th>登录时间</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `);
  } catch (e) {
    openModal("安全记录", `<div class="issues">获取失败：${esc(e.message)}</div>`);
  }
}

function openBreakdown(kind) {
  const m = state.metrics;
  if (!m) {
    openModal("各部门数据明细", '<div class="muted">暂无指标数据，请先登录后刷新。</div>');
    return;
  }
  const isAdmin = m.scope && m.scope.role === "admin";
  const titleMap = {
    skus: "SKU 总数",
    anomalies: "异常 SKU",
    llm: "LLM 调用次数 / token 量",
    queue: "巡检队列 pending / 流长度",
  };
  const title = `数据明细 · ${titleMap[kind] || kind}`;
  let headers;
  let entries;
  let totalNote;
  let rows;
  let extra = "";

  if (kind === "llm") {
    const sources = (m.llm && m.llm.sources) || {};
    const sourceLabels = { chat: "对话（ReAct 工具循环）", patrol: "巡检研判 / 校验", other: "其它（RAG/校验等）" };
    const srcRows = Object.entries(sources).map(([src, v]) => {
      const tokens = (v.input_tokens || 0) + (v.output_tokens || 0);
      return `<tr><td>${esc(sourceLabels[src] || src)}</td><td>${v.calls || 0}</td><td>${fmtTokens(tokens)}</td></tr>`;
    }).join("") || '<tr><td colspan="3" class="muted">暂无来源数据</td></tr>';
    extra = `
      <div class="section-title" style="margin-top:10px">来源分布</div>
      <table class="breakdown-table">
        <thead><tr><th>来源</th><th>调用次数</th><th>token 量</th></tr></thead>
        <tbody>${srcRows}</tbody>
      </table>
      <div class="breakdown-total">对话的调用由 chat_agent 汇总 usage 后补登记（不经过 LLMClient），因此这里的合计包含对话。</div>`;
    entries = Object.entries((m.llm && m.llm.departments) || {});
    headers = ["部门", "调用次数", "token 量"];
    rows = entries.map(([dept, v]) => {
      const tokens = (v.input_tokens || 0) + (v.output_tokens || 0);
      return `<tr><td>${esc(deptLabel(dept))}</td><td>${v.calls || 0}</td><td>${fmtTokens(tokens)}</td></tr>`;
    });
    totalNote = `全局累计 ${m.llm.calls} 次 / ${fmtTokens((m.llm.input_tokens || 0) + (m.llm.output_tokens || 0))} tok`;
  } else if (kind === "queue") {
    entries = Object.entries((m.queue && m.queue.departments) || {});
    headers = ["部门", "pending", "流长度"];
    rows = entries.map(([dept, v]) => `
      <tr><td>${esc(deptLabel(dept))}</td><td>${v.pending ?? 0}</td><td>${v.length ?? 0}</td></tr>`);
    totalNote = `全局 pending ${m.queue.pending} / 流长度 ${m.queue.length}`;
    extra = `
      <div class="breakdown-total">对话通道（SSE 进行中）：<b>${m.queue.chat_streams ?? 0}</b></div>
      <div class="breakdown-total">${esc(m.queue.note || "巡检走 Redis Stream；对话是同步 ReAct，不计入队列。")}</div>`;
  } else {
    const field = kind === "skus" ? "sku_count" : "anomaly_count";
    entries = Object.entries(m.departments || {});
    headers = ["部门", titleMap[kind]];
    rows = entries.map(([dept, v]) => `
      <tr><td>${esc(deptLabel(dept))}</td><td>${v[field] ?? "-"}</td></tr>`);
    const total = entries.reduce((sum, [, v]) => sum + (v[field] || 0), 0);
    totalNote = `${isAdmin ? "管理员视角：全部门汇总值" : "全部门汇总值"} = ${total}`;
  }

  const head = `<tr>${headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>`;
  const table = `
    <div class="section-title" style="margin-top:10px">按部门</div>
    <table class="breakdown-table">
      <thead>${head}</thead>
      <tbody>${rows.join("") || '<tr><td class="muted" colspan="' + headers.length + '">暂无数据</td></tr>'}</tbody>
    </table>`;

  if (isAdmin || !m.scope || m.scope.role === "anonymous") {
    const note = isAdmin ? "管理员视角" : "未登录（只读浏览）";
    openModal(title, `
      <div class="breakdown-total">${note}：${totalNote}</div>
      ${extra}
      ${table}
      ${!isAdmin ? '<div class="breakdown-total">研判 / 巡检等操作需登录账号；账号权限决定可见数据范围。</div>' : ""}
    `);
  } else {
    openModal(title, `
      <div class="breakdown-total">部门用户仅可查看本部门数据（${esc(deptLabel(m.scope.department))}）。</div>
      ${extra}
      ${table}
    `);
  }
}

// ---------------------------------------------------------------- 数据渲染
function resetPlaceholders() {
  $("#m-skus").textContent = "-";
  $("#m-anomalies").textContent = "-";
  $("#m-hit").textContent = "-";
  const p50 = $("#m-lat-p50");
  const p95 = $("#m-lat-p95");
  if (p50) p50.textContent = "-";
  if (p95) p95.textContent = "-";
  $("#m-llm").textContent = "-";
  $("#m-pending").textContent = "-";
  $("#label-skus").textContent = "SKU 总数";
  $("#label-anomalies").textContent = "异常 SKU";
}

function renderAnonymous() {
  $("#scope-hint").textContent = "未登录 · 只读浏览：数据正常展示；研判 / 巡检 / 安全记录等操作需登录账号";
  $("#resultCard").innerHTML = '<div class="muted">请先登录账号后发起研判。</div>';
  $("#rag-scope").textContent = "只读浏览：可检索全部 SOP（登录后按部门权限过滤）";
  setPatrolStatus("未登录：可浏览历史巡检记录，触发巡检需登录");
  $("#account-menu").classList.add("hidden");
}

function renderScopeHint(metrics) {
  if (!metrics || !metrics.scope) return;
  const role = metrics.scope.role;
  if (role === "admin") {
    $("#scope-hint").textContent = "管理员视角：全部门汇总；点击「SKU 总数」「异常 SKU」卡片可查看各部门明细。";
    $("#rag-scope").textContent = "可检索：全部 SOP（通用 + 男装部门内部 + 女装部门内部）";
  } else if (role === "user") {
    $("#scope-hint").textContent = `部门用户视角：仅显示 ${deptLabel(metrics.scope.department)} 数据。`;
    $("#rag-scope").textContent = `可检索：通用 SOP + ${deptLabel(metrics.scope.department)}内部 SOP`;
  } else {
    $("#scope-hint").textContent = "未登录 · 登录后可查看数据";
  }
}

// 存储徽标：关系库 + 向量库（Qdrant 也属于存储层，只写 mysql 会漏掉向量索引）
// memory 模式照实写 memory：它是"进程内向量检索"，不代表数据落在库里。
function storageLabel(h) {
  const db = (h && h.database) || "-";
  const backend = h && h.vector_store ? String(h.vector_store.backend || "") : "";
  return backend ? `${db}、${backend}` : db;
}

// 徽标 tooltip：把「哪个库、哪个集合、多少维、多少点、是否降级」摊开，徽标本身保持一行
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

async function loadHealth() {
  try {
    const h = await api("/api/health");
    const dbEl = $("#badge-db");
    if (dbEl) {
      dbEl.textContent = storageLabel(h);
      dbEl.title = storageTitle(h);
    }
    $("#badge-cache").textContent = h.cache;
    $("#badge-llm").textContent = h.llm_model;
    $("#badge-mcp").textContent = h.mcp_transport;
    renderSchedulerBadge(h);
  } catch (e) {
    $("#badge-db").textContent = "offline";
  }
}

function renderCacheCard(cache) {
  const el = $("#m-hit");
  if (!el) return;
  const hits = Number((cache && cache.hits) || 0);
  const misses = Number((cache && cache.misses) || 0);
  const samples = hits + misses;
  const rate = Number((cache && cache.hit_rate) || 0);
  el.textContent = samples > 0 ? `${(rate * 100).toFixed(0)}%` : "暂无采样";
  el.title = samples > 0
    ? `Agent 旁路缓存：命中 ${hits} / 未命中 ${misses}（共 ${samples} 次）`
    : "Agent 旁路缓存还没有采样：对话里调用工具取数（GET）后才会出现命中的统计";
}

function renderQueueCard(queue) {
  const pending = (queue && queue.pending) ?? 0;
  const length = (queue && queue.length) ?? 0;
  const streams = (queue && queue.chat_streams) ?? 0;
  const el = $("#m-pending");
  if (el) {
    el.textContent = `${pending} / ${length}`;
    el.title = `巡检队列：PEL 未确认 ${pending} 条，stream 中保留 ${length} 条`
      + `\n对话通道：进行中 ${streams}（对话是同步 ReAct + SSE 直推，不进队列）`;
  }
  const streamEl = $("#m-chat-streams");
  if (streamEl) streamEl.textContent = String(streams);
}

async function loadMetrics() {
  const m = await api("/api/metrics");
  state.metrics = m;
  $("#m-skus").textContent = m.sku_count;
  $("#m-anomalies").textContent = m.anomaly_count;
  $("#label-skus").textContent = "SKU 总数" + scopeSuffix(m);
  $("#label-anomalies").textContent = "异常 SKU" + scopeSuffix(m);
  renderCacheCard(m.cache);
  renderLatencyCard(m.latency);
  renderQueueCard(m.queue);
  const tokens = (m.llm.input_tokens || 0) + (m.llm.output_tokens || 0);
  $("#m-llm").textContent = `${m.llm.calls}/${fmtTokens(tokens)}`;
  const src = (m.llm && m.llm.sources) || {};
  const chatSrc = src.chat || {};
  const otherSrc = Object.entries(src).filter(([k]) => k !== "chat")
    .reduce((acc, [, v]) => ({
      calls: acc.calls + (v.calls || 0),
      tokens: acc.tokens + (v.input_tokens || 0) + (v.output_tokens || 0),
    }), { calls: 0, tokens: 0 });
  const cost = (m.llm && m.llm.cost) || {};
  const costText = typeof cost.total_cny === "number" ? `¥${cost.total_cny.toFixed(4)}` : "-";
  $("#m-llm").title = `LLM 调用 ${m.llm.calls} 次；输入 ${(m.llm.input_tokens || 0).toLocaleString()} tok，`
    + `输出 ${(m.llm.output_tokens || 0).toLocaleString()} tok（合计 ${tokens.toLocaleString()} tok）\n`
    + `来源：对话 ${chatSrc.calls || 0} 次 / ${fmtTokens((chatSrc.input_tokens || 0) + (chatSrc.output_tokens || 0))} tok；`
    + `巡检等 ${otherSrc.calls} 次 / ${fmtTokens(otherSrc.tokens)} tok\n`
    + `估算成本 ${costText}（单价 ¥${cost.price_input_per_million ?? "-"} / ¥${cost.price_output_per_million ?? "-"} 每百万 tokens，可在 .env 覆盖）`;
  renderScopeHint(m);
}

async function loadAnomalies() {
  const data = await api("/api/anomalies?limit=100");
  const tbody = $("#anomaly-rows");
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="muted">暂无异常</td></tr>';
    return;
  }
  tbody.innerHTML = "";
  for (const a of data.items) {
    const metrics = (a.reasons[0] || {}).metric || {};
    const codes = a.reasons.map((r) => r.code).join(" / ");
    const tr = document.createElement("tr");
    tr.className = "clickable";
    tr.innerHTML = `<td class="mono">${esc(a.sku_id)}</td>
      <td>${a.severity === "高" ? '<span class="tag high">高</span>' : '<span class="tag mid">中</span>'}</td>
      <td>${metrics.days_of_supply ?? "-"}</td>
      <td>${metrics.base_daily ?? "-"}</td>
      <td class="muted">${esc(codes)}</td>`;
    tr.onclick = () => {
      $("#sku-input").value = a.sku_id;
      inspectSku();
    };
    tbody.appendChild(tr);
  }
}

async function loadSkuOptions() {
  try {
    const data = await api("/api/skus?limit=200");
    const dl = $("#sku-options");
    dl.innerHTML = "";
    for (const s of data.items) {
      const opt = document.createElement("option");
      opt.value = s.sku_id;
      opt.label = `${s.style_name} ${s.color} ${s.size}`;
      dl.appendChild(opt);
    }
  } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------- 单 SKU 研判
async function inspectSku() {
  if (!state.token) { openLogin("请先登录账号后再发起研判"); return; }
  const sku = $("#sku-input").value.trim();
  if (!sku) return;
  $("#btn-inspect").disabled = true;
  $("#resultCard").innerHTML = '<div class="muted"><span class="spin"></span>Agent 运行中：采集数据 / 检索 SOP / 推理 / 校验…</div>';
  try {
    const start = await api("/api/inspect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sku_id: sku }),
    });
    await pollInspection(start.run_id);
  } catch (e) {
    $("#resultCard").innerHTML = `<div class="issues">发起失败：${esc(e.message)}</div>`;
  } finally {
    $("#btn-inspect").disabled = false;
    if (state.token) loadMetrics().catch(() => {});
  }
}

async function pollInspection(runId) {
  for (let i = 0; i < 240; i++) {
    const run = await api(`/api/inspections/${runId}`);
    if (run.status !== "running") {
      renderRun(run);
      return;
    }
    await new Promise((r) => setTimeout(r, 800));
  }
  $("#resultCard").innerHTML = '<div class="issues">超时未完成，请查看后端日志</div>';
}

function renderRun(run) {
  if (run.status === "failed" || !run.results.length) {
    $("#resultCard").innerHTML = `<div class="issues">研判失败：${esc(run.error || "无结果")}</div>`;
    return;
  }
  const r = run.results[0];
  const plan = r.plan || {};
  const bundle = r.data_bundle || {};
  const judgment = bundle.judgment || {};
  const replenish = bundle.replenish || {};
  const trace = bundle.trace || [];
  const verifier = r.verifier || {};
  const ragCitations = bundle.rag_citations || [];
  const cited = new Set(plan.citations || []);

  const actions = (plan.plan || []).map((a) => `
    <tr><td>${esc(a.action)}</td><td>${a.qty ?? "-"}</td><td>${a.eta_days ?? "-"}</td><td>${esc(a.reason)}</td></tr>`).join("");

  const compliance = Object.entries(verifier.compliance || {}).map(([k, v]) => `
    <div class="check ${v ? "ok" : "bad"}"><div class="k">${esc(k)}</div><div class="v">${v ? "通过" : "未通过"}</div></div>`).join("");

  const issues = (verifier.issues || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const fixes = (verifier.fixes || []).map((x) => `<li>${esc(x)}</li>`).join("");

  const citations = ragCitations.map((c) => `
    <div class="citation">
      <span class="cid">[${esc(c.chunk_id)}]</span><b>${esc(c.title)}</b>
      <span class="tag gray">${esc(c.department || (c.meta && c.meta.department) || "")}</span>
      ${cited.has(c.chunk_id) ? '<span class="tag low" style="margin-left:6px">已引用</span>' : ""}
      <div class="snippet">${esc(c.snippet || "")}</div>
    </div>`).join("");

  const timeline = trace.map((t) => `
    <div class="step"><span class="n">${esc(t.node)}</span>
      <span>${t.status === "ok" || t.status === "passed" ? "✓" : t.status === "degraded" || t.status === "skipped" ? "△" : "✗"} ${esc(t.detail || "")} (${t.ms}ms)</span></div>`).join("");

  const flags = (judgment.risk_flags || []).map((f) => `<span class="tag mid">${esc(f)}</span>`).join("");

  $("#resultCard").innerHTML = `
    <div class="result-head">
      <span class="sku mono">${esc(r.sku_id)}</span>
      ${riskTag(r.risk_level)}
      ${r.degraded ? '<span class="tag gray">降级预案</span>' : ""}
      <span class="meta">${run.kind === "single" ? "单 SKU 研判" : "批量巡检"} · 耗时 ${(r.elapsed_ms / 1000).toFixed(1)}s · ${esc(r.created_at)}</span>
    </div>
    <div class="verdict">${esc(plan.verdict || "")}</div>

    <div class="section-title">数据证据</div>
    <ul class="evidence">${(plan.evidence || []).map((e) => `<li>${esc(e)}</li>`).join("")}</ul>

    <div class="section-title">处置预案</div>
    <table>
      <thead><tr><th>动作</th><th>数量</th><th>预计天数</th><th>依据</th></tr></thead>
      <tbody>${actions}</tbody>
    </table>

    <div class="section-title">Skill 研判（子图输出）</div>
    <div class="muted">
      风险标签：${flags || "无"} ｜ 库存健康度 ${judgment.health_score ?? "-"} ｜ 可售 ${judgment.days_of_supply ?? "-"} 天 ｜
      补货测算建议 <b class="mono">${replenish.suggested_qty ?? "-"}</b> 件（MOQ ${replenish.moq ?? "-"}，交期 ${replenish.lead_time_days ?? "-"} 天）
      ${judgment.promo_window_near ? " ｜ <b>30 天内有大促窗口</b>" : ""}
    </div>

    <div class="section-title">引用溯源（RAG TopK）</div>
    <div class="citations">${citations || '<div class="muted">无引用</div>'}</div>

    <div class="section-title">校验 Agent 结论</div>
    <div class="compliance">${compliance}</div>
    ${issues ? `<div class="section-title">未通过项</div><ul class="issues">${issues}</ul>` : ""}
    ${fixes ? `<div class="section-title">修正建议</div><ul class="fixes">${fixes}</ul>` : ""}

    <div class="section-title">执行轨迹</div>
    <div class="timeline">${timeline}</div>
  `;
}

// ---------------------------------------------------------------- 批量巡检
const PATROL_IDLE_TEXT = "暂无正在进行的巡检（最近 10 次见下表）";

function setPatrolStatus(text) {
  const box = $("#patrol-status");
  if (box) box.textContent = text || PATROL_IDLE_TEXT;
}

function renderPatrolProgress(p) {
  const box = $("#patrol-status");
  if (!box) return;
  if (!p || !p.stage) { box.textContent = PATROL_IDLE_TEXT; return; }
  const stageText = {
    init: "初始化", screening: "量化筛查", enqueue: "任务入队", consume: "消费队列",
    inspect_start: "开始研判", inspect: "研判中", finished: "已完成", cancelled: "已终止",
  }[p.stage] || p.stage;
  const parts = [`阶段：${stageText}`];
  if (p.sku_id) parts.push(`当前 SKU：${p.sku_id}`);
  if (p.node) parts.push(`链路节点：${p.node}`);
  if (p.total_selected) parts.push(`进度：${p.processed || 0}/${p.total_selected}`);
  if (p.queue && p.queue.pending !== undefined) parts.push(`队列 pending：${p.queue.pending} / 流长度 ${p.queue.length ?? "-"}`);
  parts.push(`已用时：${((p.elapsed_ms || 0) / 1000).toFixed(1)}s`);
  const params = p.params || {};
  if (params.mode) parts.push(`参数：${params.mode} / limit=${params.limit ?? "全部"} / ${params.department || "全部门"}`);
  box.textContent = `${p.message || ""}\n${parts.join(" · ")}`;
}

async function cancelPatrol() {
  if (!state.token) { openLogin("请先登录账号后再终止巡检"); return; }
  if (!patrolState.runId || !patrolState.running) return;
  $("#btn-patrol-cancel").disabled = true;
  try {
    await api(`/api/patrol/${patrolState.runId}/cancel`, { method: "POST" });
    setPatrolStatus((($("#patrol-status") || {}).textContent || "")
      .trim() + "\n已发送终止信号，将在当前批次结束后停止。");
  } catch (e) {
    $("#btn-patrol-cancel").disabled = false;
    setPatrolStatus(`终止失败：${e.message}`);
  }
}

async function runPatrol() {
  if (!state.token) { openLogin("请先登录账号后再触发巡检"); return; }
  const mode = $("#patrol-mode").value;
  const limit = Number($("#patrol-limit").value || 8);
  $("#btn-patrol").disabled = true;
  $("#btn-patrol-cancel").disabled = true;
  setPatrolStatus("巡检任务已提交，等待执行…");
  try {
    const start = await api("/api/patrol", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, limit }),
    });
    patrolState.runId = start.run_id;
    patrolState.running = true;
    $("#btn-patrol-cancel").disabled = false;
    for (let i = 0; i < 900; i++) {
      const run = await api(`/api/patrol/${start.run_id}`);
      renderPatrolProgress(run.progress || {});
      if (run.status !== "running") {
        const s = run.stats || {};
        setPatrolStatus([
          `run=${run.run_id}`,
          `status=${run.status}`,
          `screened=${s.screened ?? "-"} skipped_recent=${s.skipped_recent ?? 0} candidates=${s.candidates ?? "-"} selected=${s.selected ?? "-"} enqueued=${s.enqueued ?? "-"} deduped=${s.deduped ?? "-"}`,
          `processed=${s.processed ?? "-"} ok=${s.ok ?? "-"} degraded=${s.degraded ?? "-"} failed=${s.failed ?? "-"}`,
          s.department ? `department=${s.department}` : "",
          `elapsed=${s.elapsed_ms ? (s.elapsed_ms / 1000).toFixed(1) + "s" : "-"}`,
          run.error ? `error=${run.error}` : "",
        ].filter(Boolean).join("\n"));
        break;
      }
      setPatrolStatus(`巡检中… run=${start.run_id} 已等待 ${(i + 1) * 2}s`);
      await new Promise((r) => setTimeout(r, 2000));
    }
  } catch (e) {
    setPatrolStatus(`巡检失败：${e.message}`);
  } finally {
    patrolState.running = false;
    patrolState.runId = null;
    $("#btn-patrol").disabled = false;
    $("#btn-patrol-cancel").disabled = true;
    if (state.token) loadMetrics().catch(() => {});
    if (state.token) loadAnomalies().catch(() => {});
    if (state.token) loadPatrolLogs().catch(() => {});
  }
}

// ---------------------------------------------------------------- RAG 检索
// 权威度中英映射：与后端 app/common/conflicts.py 的 AUTHORITY_LABEL 保持一致
const RAG_AUTHORITY_LABEL = {
  governance: "治理口径", dept: "部门细则", general: "通用规则", case: "历史案例",
};

// 口径冲突区块：检索到两份互相矛盾的制度时，面板要直接显示「采用了哪条、为什么」。
// 不显示的话，用户只会在回答里看到某一个数字，却不知道它压过了另一份 SOP。
// 渲染顺序与提示词段落一致：范围说明 → 冲突 → 语料内部不一致 → 部门例外 → 单一来源提示。
function ragConflictBlock(data) {
  const conflicts = data.conflicts || [];
  const diag = data.conflict_diagnostics || {};
  const overrides = data.department_overrides || [];
  const uncovered = data.uncovered_departments || [];
  const singleSource = diag.single_source_keys || [];
  const notice = data.scope_notice || "";
  const noticeBlock = notice
    ? `<div class="citation"><b>检索范围说明</b>`
      + `<span class="muted">先告知用户，再作答</span>`
      + `<div class="snippet">${esc(notice)}</div></div>`
    : "";
  const overrideBlock = overrides.length || uncovered.length
    ? `<div class="citation"><b>部门例外</b>`
      + '<span class="muted">不参与裁决，只作补充说明</span>'
      + overrides.map((o) => `<div class="snippet">若问的是 <b>${esc(o.department || "")}</b> 部门，`
        + `则为 ${esc(o.display || "")}（${esc(o.title || "")}`
        + `<span class="tag gray">${esc(RAG_AUTHORITY_LABEL[o.authority] || o.authority || "")}</span>）</div>`).join("")
      + (uncovered.length
        ? `<div class="snippet muted">未单独规定的部门（${esc(uncovered.join("、"))}）按通用口径执行</div>`
        : "")
      + `</div>`
    : "";
  const singleHint = (data.conflict_check === "none" && singleSource.length)
    ? `<div class="muted" style="margin-bottom:8px">`
      + `本次比对了 ${(diag.compared_keys || []).length} 个相关口径，未发现分歧；`
      + `其中 ${singleSource.length} 个口径仅有单一来源（${esc(singleSource.slice(0, 3).join("、"))}），未做交叉验证`
      + `</div>`
    : "";
  if (!conflicts.length) {
    if (data.conflict_check === "unavailable") {
      return noticeBlock + overrideBlock
        + '<div class="muted" style="margin-bottom:8px">'
        + '口径冲突检查不可用（本次召回片段未声明可比对的口径，不能据此认为没有冲突）</div>';
    }
    return noticeBlock + singleHint + overrideBlock;
  }
  const cross = conflicts.filter((c) => (c.kind || "cross_doc") === "cross_doc");
  const intra = conflicts.filter((c) => c.kind === "intra_doc");
  const crossRows = cross.map((c) => {
    const w = c.winner || {};
    const loserText = (c.losers || [])
      .map((l) => `${esc(l.display)}（${esc(l.title)}）`).join("、");
    return `<div class="snippet">采用 <b>${esc(w.display)}</b> · ${esc(w.title)} `
      + `<span class="tag gray">${esc(RAG_AUTHORITY_LABEL[w.authority] || w.authority || "")}</span>`
      + ` ← ${esc(c.reason || "")}${loserText ? `；不采用 ${loserText}` : ""}</div>`;
  }).join("");
  const intraRows = intra.map((c) => `<div class="snippet">⚠️ ${esc(c.title || c.doc_id)} 对 `
    + `<b>${esc(c.key)}</b> 同时声明了 ${esc((c.values || []).join(" / "))}`
    + '（语料需修订，不按其中任一条作答）</div>').join("");
  const head = cross.length
    ? `<b>口径冲突 ${cross.length} 处</b>`
      + '<span class="muted">已按「治理口径 &gt; 部门细则 &gt; 通用规则 &gt; 历史案例」裁决，回答只用胜出口径</span>'
    : '<b>语料内部不一致</b><span class="muted">同一文档前后口径矛盾，需修订文档</span>';
  return noticeBlock
    + `<div class="citation">${head}${crossRows}${intraRows}</div>`
    + singleHint + overrideBlock;
}

async function ragSearch() {
  const q = $("#rag-input").value.trim();
  if (!q) return;
  $("#btn-rag").disabled = true;
  $("#rag-results").innerHTML = '<div class="muted"><span class="spin"></span>检索中…</div>';
  try {
    const data = await api(`/api/rag/search?q=${encodeURIComponent(q)}&topk=5`);
    const loserDocs = new Set(
      (data.conflicts || []).flatMap((c) => (c.losers || []).map((l) => l.doc_id)),
    );
    const hits = data.hits.map((h) => {
      const authority = (h.meta && h.meta.authority) || "";
      const secondary = loserDocs.has(h.doc_id) ? '<span class="tag gray">次要口径</span>' : "";
      return `
      <div class="citation">
        <span class="cid">[${esc(h.chunk_id)}]</span><b>${esc(h.title)}</b> · ${esc(h.section)}
        <span class="tag gray">${esc((h.meta && h.meta.department) || "")}</span>
        ${authority ? `<span class="tag gray">${esc(RAG_AUTHORITY_LABEL[authority] || authority)}</span>` : ""}
        ${secondary}
        <span class="muted">score ${h.score}</span>
        <div class="snippet">${esc(h.text || h.snippet || "")}</div>
      </div>`;
    }).join("") || '<div class="muted">无结果</div>';
    $("#rag-results").innerHTML = ragConflictBlock(data) + hits;
    if (data.scope) $("#rag-scope").textContent = `可检索：${data.scope}`;
  } catch (e) {
    $("#rag-results").innerHTML = `<div class="issues">${esc(e.message)}</div>`;
  } finally {
    $("#btn-rag").disabled = false;
  }
}

// ---------------------------------------------------------------- SKU 搜索
async function searchSkus() {
  const q = $("#sku-search").value.trim();
  try {
    const data = await api(`/api/skus?q=${encodeURIComponent(q)}&limit=30`);
    $("#sku-rows").innerHTML = data.items.map((s) => `
      <tr class="clickable">
        <td class="mono">${esc(s.sku_id)}</td>
        <td>${esc(s.style_name)} ${esc(s.color)} ${esc(s.size)}</td>
        <td>${s.stock}</td>
        <td class="muted">${esc(s.warehouse_id)}</td>
      </tr>`).join("");
    for (const tr of document.querySelectorAll("#sku-rows tr")) {
      tr.onclick = () => {
        const sku = tr.querySelector("td").textContent.trim();
        $("#sku-input").value = sku;
        inspectSku();
      };
    }
  } catch (e) {
    $("#sku-rows").innerHTML = `<tr><td colspan="4" class="issues">${esc(e.message)}</td></tr>`;
  }
}

// ---------------------------------------------------------------- 初始化
async function refreshData() {
  try {
    await loadMetrics();
    await loadAnomalies();
    await loadSkuOptions();
    await searchSkus();
    await loadPendingApprovals();
    await loadPatrolLogs();
    await loadChatSessions();
  } catch (e) { /* 401 已在 api() 处理 */ }
}

// ---------------------------------------------------------------- AI 对话 / 待人工确认
function appendChat(role, text, trace, opts = {}) {
  const box = $("#chat-messages");
  const div = document.createElement("div");
  div.className = `chat-msg ${role}`;
  div.textContent = text;
  // 已有结构化「运行详情」时不再重复展示原始工具轨迹（避免双份日志噪音）
  if (trace && trace.length && !opts.skipTrace) {
    const details = document.createElement("details");
    details.className = "chat-tools";
    const summary = document.createElement("summary");
    summary.textContent = `工具调用轨迹（${trace.filter(t => t.type === "call").length} 次）`;
    const pre = document.createElement("pre");
    pre.textContent = trace.map(t => t.type === "call"
      ? `→ ${t.tool}(${JSON.stringify(t.args)})`
      : `← ${t.tool}: ${String(t.result).slice(0, 400)}`).join("\n");
    details.appendChild(summary);
    details.appendChild(pre);
    div.appendChild(details);
  }
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

// ---------------------------------------------------------------- 真·流式：增量气泡
function appendStreamDelta(text) {
  if (!text) return null;
  let bubble = chatState.streamBubble;
  if (!bubble) {
    const box = $("#chat-messages");
    if (!box) return null;
    bubble = document.createElement("div");
    bubble.className = "chat-msg assistant streaming";
    bubble.textContent = "";
    box.appendChild(bubble);
    chatState.streamBubble = bubble;
    // 答案开始逐字输出后，进度卡就该让位（工具/阶段信息在 done 后的运行详情里仍有全量记录）
    const prog = chatState.progressEl;
    if (prog && prog.remove) prog.remove();
    chatState.progressEl = null;
  }
  bubble.textContent = String(bubble.textContent || "") + text;
  const box = $("#chat-messages");
  if (box) box.scrollTop = box.scrollHeight;
  return bubble;
}

function takeStreamBubble() {
  const bubble = chatState.streamBubble;
  chatState.streamBubble = null;
  return bubble;
}

// 撤回已推的增量（模型"先说话、后决定调工具"时，那段不是最终答案）。
// 只清空气泡内容，不删节点：后续 delta 会继续往同一个气泡里写最终答案。
function clearStreamBubble() {
  const bubble = chatState.streamBubble;
  if (!bubble) return null;
  bubble.textContent = "";
  return bubble;
}

function finalizeStreamBubble(bubble, reply) {
  if (!bubble) return null;
  if (bubble.classList) bubble.classList.remove("streaming");
  bubble.textContent = String(reply || "");
  const box = $("#chat-messages");
  if (box) box.scrollTop = box.scrollHeight;
  return bubble;
}

// 流式中途失败：保留已生成的部分，但必须显式标注"未完成、未校验"，
// 否则用户会把半截答案当成完整结论。
function markStreamBubbleIncomplete(bubble) {
  if (!bubble || !bubble.appendChild) return bubble;
  const note = document.createElement("div");
  note.className = "chat-warn";
  note.textContent = "（回答未完成：上面是中断前已生成的部分，未经出口溯源校验）";
  bubble.appendChild(note);
  const box = $("#chat-messages");
  if (box) box.scrollTop = box.scrollHeight;
  return bubble;
}

function resetChatState(message) {
  chatState.sessionId = null;
  chatState.sending = false;
  const sel = $("#chat-history");
  if (sel) sel.value = "";
  const box = $("#chat-messages");
  if (box) {
    box.innerHTML = `<div class="muted">${message || "登录后可对话：支持问数、缺货诊断与补货草稿单（大额补货需人工确认）。"}</div>`;
  }
  setFactsUnderbar({ card: "", meta: "登录并发起一轮对话后，这里会显示当前会话的事实卡", empty: "—" });
  setWorkingSetUnderbar({ rows: [], text: "", meta: "登录并发起一轮对话后显示本会话实体表", empty: "—" });
}

async function sendChat(messageOverride, opts = {}) {
  if (!state.token) { openLogin("请先登录账号后再对话"); return; }
  if (chatState.sending) return;
  const input = $("#chat-input");
  const message = (messageOverride || input.value || "").trim();
  if (!message) return;

  const replay = Boolean(opts.replay);
  if (!replay) appendChat("user", message);
  input.value = "";
  let typing = createProgressCard();
  $("#chat-messages").appendChild(typing.el);
  chatState.progressEl = typing.el;
  chatState.streamBubble = null;
  chatState.sending = true;
  let retried = Boolean(opts.retried);
  try {
    for (;;) {
      try {
        const data = await requestChat(message, typing);
        LAST_CHAT_DEBUG = { at: new Date().toLocaleTimeString(), keys: Object.keys(data || {}),
                            hasRunDetail: Boolean(data && data.run_detail),
                            hasQueryPlan: Boolean(data && data.query_plan) };
        chatState.sessionId = data.session_id;
        typing.remove();
        chatState.progressEl = null;
        if (data.query_plan) appendQueryPlanCard(data.query_plan, data.clarify);
        // 流式时答案已经在气泡里逐字显示过：直接用最终答复覆盖（溯源校验可能改写过）
        const streamed = finalizeStreamBubble(takeStreamBubble(), data.reply);
        const bubble = streamed || appendChat("assistant", data.reply, data.tool_trace || [],
                                              { skipTrace: Boolean(data.run_detail) });
        // 流式输出的是初稿：出口溯源校验可能改写过它（或标记为降级），如实告诉用户
        if (data.streamed_unverified) {
          appendWarnNote("提示：流式输出的是初稿，最终答案经溯源自检修正（数字与来源以最终答案为准）。");
        }
        attachRunDetail(bubble, data.run_detail, { open: true, planCardShown: Boolean(data.query_plan) });
        // 失败可见化：数据缺失时显式提示，而不是"安静地什么都不渲染"
        if (!data.run_detail) {
          appendWarnNote("本轮未收到运行详情（run_detail）：后端版本可能落后，或响应被裁剪。点「自检」查看键名与版本。");
        }
        if (chatState.queryPlan && !data.query_plan) {
          appendWarnNote("已勾选 Query 重写，但本轮未返回 query_plan（后端未支持该字段）。");
        }
        if (data.run_detail) {
          updateFactsUnderbarFromDetail(data.run_detail);
          updateWorkingSetUnderbarFromDetail(data.run_detail);
        } else {
          await refreshFactsUnderbar();   // run_detail 缺失时兜底：直接读记忆接口
        }
        appendCitedNotice(data.cited_result_ids);
        const msgBox = $("#chat-messages");
        if (msgBox) msgBox.scrollTop = msgBox.scrollHeight;
        loadChatSessions();  // 新会话/新标题进入历史下拉
        await loadPendingApprovals();
        return;
      } catch (e) {
        const msg = e.message || "";
        const sessionLost = msg.includes("会话不存在") || msg.includes("无权访问");
        if (!retried && sessionLost) {
          // 会话属于原账号或已失效（常见于切换账号后）：自动开启新会话并重试
          chatState.sessionId = null;
          retried = true;
          typing.remove();
          takeStreamBubble();   // 丢弃半截气泡，避免重复
          appendChat("system", "当前会话不可用（可能切换了账号），已自动开启新会话并重试。");
          typing = createProgressCard();
          $("#chat-messages").appendChild(typing.el);
          chatState.progressEl = typing.el;
          continue;
        }
        typing.remove();
        chatState.progressEl = null;
        // 流式已输出的半截内容保留（失败前已生成的部分不丢），空气泡则清掉；
        // 保留下来的半截必须标注"未完成"，否则用户会当成完整答案
        const partial = takeStreamBubble();
        if (partial && !String(partial.textContent || "").trim() && partial.remove) {
          partial.remove();
        } else if (partial) {
          markStreamBubbleIncomplete(partial);
        }
        appendChat("system", "对话失败：" + (msg || "未知错误"));
        return;
      }
    }
  } finally {
    chatState.sending = false;
  }
}

// 「新会话」= 开一个新的对话，不清空旧会话（旧会话仍可在「历史会话」里回放）
async function resetChat() {
  chatState.sessionId = null;
  const sel = $("#chat-history");
  if (sel) sel.value = "";
  const box = $("#chat-messages");
  if (box) {
    box.innerHTML = '<div class="muted">已开启新会话。之前聊过的会话保留在「历史会话」下拉里，可随时回放（含当时的运行详情与事实卡快照）。</div>';
  }
  setFactsUnderbar({ card: "", meta: "新会话：还没有事实卡，聊到第 2 轮开始自动生成", empty: "—" });
  setWorkingSetUnderbar({ rows: [], text: "", meta: "新会话：还没有实体（提问里出现 SKU/款号/尺码后自动累积）", empty: WORKING_SET_EMPTY });
  await loadChatSessions();
}

const ACTION_STATUS_LABEL = {
  pending_confirmation: "待确认",
  approved: "已确认",
  rejected: "已驳回",
  auto_approved: "自动通过",
};

async function loadPendingApprovals() {
  await Promise.all([loadReplenishOrders(), loadAfterSalesTickets()]);
}

async function loadReplenishOrders() {
  const tbody = $("#approval-rows");
  if (!state.token) {
    tbody.innerHTML = '<tr><td colspan="5" class="muted">登录后可查看补货单列表（近 4 小时）</td></tr>';
    return;
  }
  try {
    const data = await api("/api/actions/list");
    if (!data.items.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted">近 4 小时暂无补货单。可在「AI 助手」生成补货草稿，或点上方演示按钮。</td></tr>';
      return;
    }
    tbody.innerHTML = "";
    for (const it of data.items) {
      const label = ACTION_STATUS_LABEL[it.status] || it.status;
      const cls = it.status === "pending_confirmation" ? "mid" : (it.status === "rejected" ? "high" : "low");
      const pending = it.status === "pending_confirmation";
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="mono">${esc(it.sku_id)}</td>
        <td>${it.qty}</td>
        <td>¥${(it.amount || 0).toLocaleString()}</td>
        <td><span class="tag ${cls}">${esc(label)}</span><div class="muted">${esc((it.reason || "").slice(0, 26))}</div></td>
        <td><button class="mini-btn" data-detail="${esc(it.approval_id)}">详情</button>
            ${pending ? `<button class="mini-btn" data-id="${esc(it.approval_id)}" data-action="approve">确认</button>
            <button class="mini-btn danger" data-id="${esc(it.approval_id)}" data-action="reject">驳回</button>` : ""}</td>`;
      tbody.appendChild(tr);
    }
    tbody.querySelectorAll("button[data-action]").forEach(btn => {
      btn.onclick = () => decideAction(btn.dataset.id, btn.dataset.action);
    });
    tbody.querySelectorAll("button[data-detail]").forEach(btn => {
      btn.onclick = () => {
        const item = data.items.find(x => String(x.approval_id) === btn.dataset.detail);
        if (item) openApprovalDetail(item);
      };
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="issues">${esc(e.message)}</td></tr>`;
  }
}

const AFTER_SALES_LABEL = {
  pending_confirmation: "待确认",
  approved: "已确认",
  rejected: "已驳回",
};

async function loadAfterSalesTickets() {
  const tbody = $("#after-sales-rows");
  if (!tbody) return;
  if (!state.token) {
    tbody.innerHTML = '<tr><td colspan="6" class="muted">登录后可查看售后工单（待确认 / 已处理）</td></tr>';
    return;
  }
  try {
    const data = await api("/api/actions/after-sales?status=all&limit=20");
    if (!data.items.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="muted">暂无售后工单。可在「AI 助手」问「订单 xx 要退货怎么办」让 Agent 生成草稿。</td></tr>';
      return;
    }
    tbody.innerHTML = "";
    for (const it of data.items) {
      const pending = it.status === "pending_confirmation";
      const cls = pending ? "mid" : (it.status === "rejected" ? "high" : "low");
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="mono">${esc(it.order_id)}</td>
        <td class="mono">${esc(it.sku_id)}</td>
        <td>${esc(it.reason || "-")}</td>
        <td>${it.qty}</td>
        <td><span class="tag ${cls}">${esc(AFTER_SALES_LABEL[it.status] || it.status)}</span>
            <div class="muted">${esc(it.approver ? `${it.approver} · ${(it.decided_at || "").replace("T", " ")}` : "（待确认）")}</div></td>
        <td>${pending ? `<button class="mini-btn" data-ticket="${esc(it.ticket_id)}" data-action="approve">确认</button>
            <button class="mini-btn danger" data-ticket="${esc(it.ticket_id)}" data-action="reject">驳回</button>`
            : `<button class="mini-btn" data-ticket-detail="${esc(it.ticket_id)}">详情</button>`}</td>`;
      tbody.appendChild(tr);
    }
    tbody.querySelectorAll("button[data-action]").forEach(btn => {
      btn.onclick = () => decideAfterSales(btn.dataset.ticket, btn.dataset.action);
    });
    tbody.querySelectorAll("button[data-ticket-detail]").forEach(btn => {
      btn.onclick = () => {
        const item = data.items.find(x => String(x.ticket_id) === btn.dataset.ticketDetail);
        if (item) openAfterSalesDetail(item);
      };
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="issues">${esc(e.message)}</td></tr>`;
  }
}

async function decideAfterSales(ticketId, action) {
  if (!state.token) { openLogin("请先登录账号后再审批"); return; }
  let body = {};
  if (action === "approve") {
    if (!window.confirm("确认该售后工单？（演示环境不会真正退款）")) return;
  } else {
    body = { reason: window.prompt("驳回原因：") || "" };
  }
  try {
    const result = await api(`/api/actions/after-sales/${ticketId}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await loadAfterSalesTickets();
    appendChat("system", `售后工单 ${result.ticket_id.slice(0, 8)}… 已${action === "approve" ? "确认" : "驳回"}（操作人 ${result.operator}）。`);
  } catch (e) {
    window.alert("操作失败：" + (e.message || "未知错误"));
  }
}

function openAfterSalesDetail(it) {
  openModal("售后工单详情", `
    <table class="breakdown-table">
      <tbody>
        <tr><th>工单号</th><td class="mono">${esc(it.ticket_id)}</td></tr>
        <tr><th>订单号</th><td class="mono">${esc(it.order_id)}</td></tr>
        <tr><th>SKU</th><td class="mono">${esc(it.sku_id)}</td></tr>
        <tr><th>原因</th><td>${esc(it.reason || "-")}</td></tr>
        <tr><th>数量</th><td>${it.qty}</td></tr>
        <tr><th>状态</th><td>${esc(AFTER_SALES_LABEL[it.status] || it.status)}</td></tr>
        <tr><th>审批账号</th><td>${esc(it.approver || "（待确认）")}</td></tr>
        <tr><th>确认/驳回时间</th><td>${esc((it.decided_at || "-").replace("T", " "))}</td></tr>
        <tr><th>批注</th><td>${esc(it.note || "-")}</td></tr>
        <tr><th>发起时间</th><td>${esc((it.created_at || "-").replace("T", " "))}</td></tr>
      </tbody>
    </table>
    <div class="artifact-foot">↑ 售后工单同样遵循「只建草稿 + 人工确认」：确认不会触发真实退款。</div>
  `);
}

function openApprovalDetail(it) {
  const label = ACTION_STATUS_LABEL[it.status] || it.status;
  openModal("补货单详情", `
    <div class="security-note">该列表仅保留近 4 小时的补货单记录，超出后自动不再展示。</div>
    <table class="breakdown-table">
      <tbody>
        <tr><th>SKU</th><td class="mono">${esc(it.sku_id)}</td></tr>
        <tr><th>仓库</th><td>${esc(it.warehouse_id || "-")}</td></tr>
        <tr><th>当前可用库存</th><td><b>${it.current_available ?? "-"}</b> 件（现存 ${it.current_on_hand ?? "-"} 件，日均销量 ${it.daily_avg_7d ?? "-"}）</td></tr>
        <tr><th>采购件数</th><td><b>${it.qty}</b> 件</td></tr>
        <tr><th>金额</th><td>¥${(it.amount || 0).toLocaleString()}</td></tr>
        <tr><th>状态</th><td>${esc(label)}（ERP：${esc(it.erp_status || "-")}）</td></tr>
        <tr><th>审批账号</th><td>${esc(it.approver || "（待确认）")}</td></tr>
        <tr><th>确认/驳回时间</th><td>${esc(it.decided_at || "-")}</td></tr>
        <tr><th>发起时间</th><td>${esc(it.created_at || "-")}</td></tr>
        <tr><th>采购单号</th><td class="mono">${esc(it.po_no || "-")}</td></tr>
        <tr><th>发起原因</th><td>${esc(it.reason || "-")}</td></tr>
      </tbody>
    </table>
  `);
}

async function loadPatrolLogs() {
  const tbody = $("#patrol-log-rows");
  const reportBox = $("#patrol-report");
  if (!tbody) return;
  if (!state.token) {
    tbody.innerHTML = '<tr><td colspan="7" class="muted">登录后可查看巡检日志与简报</td></tr>';
    if (reportBox) reportBox.innerHTML = "";
    return;
  }
  try {
    const data = await api("/api/patrol/recent?limit=10");
    if (!data.items.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="muted">暂无巡检记录（登录后触发批量巡检即可生成）</td></tr>';
      if (reportBox) reportBox.innerHTML = "";
      return;
    }
    tbody.innerHTML = "";
    for (const it of data.items) {
      const tr = document.createElement("tr");
      tr.className = "clickable";
      tr.innerHTML = `<td>${esc((it.created_at || "").replace("T", " "))}</td>
        <td>${esc(it.operator)}</td><td>${esc(it.mode)}</td><td>${esc(it.department || "全部门")}</td>
        <td>${it.processed}</td><td>${it.failed}</td>
        <td><button class="mini-btn" data-run="${esc(it.run_id)}">查看报告</button></td>`;
      tr.onclick = () => openPatrolReport(it);
      tbody.appendChild(tr);
    }
    renderPatrolReport(data.items[0]);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="issues">${esc(e.message)}</td></tr>`;
  }
}

function renderPatrolReport(log) {
  const box = $("#patrol-report");
  if (!box) return;
  if (!log) { box.innerHTML = ""; return; }
  const report = log.report || {};
  box.innerHTML = `
    <div class="section-title">本次巡检简报（${esc((log.created_at || "").replace("T", " "))} · ${esc(log.operator)} · ${esc(log.mode)}）</div>
    <div class="muted" style="line-height:1.8">${esc(report.summary || "无简报内容")}</div>`;
}

function openPatrolReport(log) {
  const report = log.report || {};
  const top = (report.top_actions || []).map(a =>
    `<li>${esc(a.sku_id)}：${esc(a.risk_level)}风险 → ${esc(a.action)}${a.qty ? " " + a.qty + " 件" : ""}</li>`
  ).join("");
  openModal("巡检报告", `
    <div class="section-title">简报</div>
    <div class="muted" style="line-height:1.8">${esc(report.summary || "")}</div>
    <div class="section-title">关键数据</div>
    <table class="breakdown-table"><tbody>
      <tr><th>操作人</th><td>${esc(log.operator)}</td></tr>
      <tr><th>模式 / 部门</th><td>${esc(log.mode)} / ${esc(log.department || "全部门")}</td></tr>
      <tr><th>筛查 → 候选 → 处理</th><td>${log.screened} → ${log.candidates} → ${log.selected}（成功 ${log.ok}，降级 ${log.degraded}，失败 ${log.failed}）</td></tr>
      <tr><th>跳过（最近 1h 已处理）</th><td>${log.skipped_recent}</td></tr>
      <tr><th>耗时</th><td>${(log.elapsed_ms / 1000).toFixed(1)}s</td></tr>
    </tbody></table>
    ${top ? `<div class="section-title">优先关注</div><ul class="evidence">${top}</ul>` : ""}
    ${report.next_steps ? `<div class="section-title">后续流程</div><div class="muted">${esc(report.next_steps)}</div>` : ""}
  `);
}

async function decideAction(approvalId, action) {
  if (!state.token) { openLogin("请先登录账号后再审批"); return; }
  let body = {};
  if (action === "approve") {
    if (!window.confirm("确认通过该补货草稿单？通过后 Mock ERP 将生成正式采购单。")) return;
  } else {
    body = { reason: window.prompt("驳回原因：") || "" };
  }
  try {
    await api(`/api/actions/${approvalId}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    await loadPendingApprovals();
  } catch (e) {
    window.alert("操作失败：" + (e.message || "未知错误"));
  }
}

function bindEvents() {
  $("#btn-login").onclick = () => openLogin("");
  $("#account-chip").onclick = (e) => {
    e.stopPropagation();
    $("#account-menu").classList.toggle("hidden");
  };
  $("#login-form").onsubmit = submitLogin;
  $("#login-cancel").onclick = closeLogin;
  $("#login-modal").onclick = (e) => { if (e.target.id === "login-modal") closeLogin(); };
  $("#modal-close").onclick = closeModal;
  $("#modal-overlay").onclick = (e) => { if (e.target.id === "modal-overlay") closeModal(); };

  $("#menu-switch").onclick = async () => {
    $("#account-menu").classList.add("hidden");
    try { await api("/api/auth/logout", { method: "POST" }); } catch (e) { /* ignore */ }
    clearToken();
    renderAccount();
    renderAnonymous();
    resetChatState("已切换账号，请登录新的账号后开始对话。");
    openLogin("请登录新的账号");
  };
  $("#menu-security").onclick = () => {
    $("#account-menu").classList.add("hidden");
    if (!state.token) { openLogin("请先登录账号查看安全记录"); return; }
    openSecurityModal();
  };
  $("#menu-logout").onclick = doLogout;

  document.addEventListener("click", (e) => {
    if (!e.target.closest || !e.target.closest("#account")) {
      $("#account-menu").classList.add("hidden");
    }
  });

  $("#card-skus").onclick = () => openBreakdown("skus");
  $("#card-anomalies").onclick = () => openBreakdown("anomalies");
  $("#card-llm").onclick = () => openBreakdown("llm");
  $("#card-queue").onclick = () => openBreakdown("queue");
  const hitCard = $("#card-hit");
  if (hitCard) hitCard.onclick = openLatencyBreakdown;

  $("#btn-chat-send").onclick = () => sendChat();
  $("#chat-input").addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(); });
  $("#btn-chat-reset").onclick = resetChat;
  initQueryPlanToggle();
  initRunDetailToggle();
  renderFeatureHints();
  document.querySelectorAll(".chip-btn").forEach((btn) => {
    btn.onclick = () => sendChat(btn.dataset.example);
  });
  $("#btn-refresh-approvals").onclick = loadPendingApprovals;
  $("#btn-approval-demo").onclick = () => {
    if (!state.token) { openLogin("请先登录账号后再生成演示草稿"); return; }
    const example = "ST-02-C1-M 可用库存为 0，请生成补货草稿单，数量按 300 件创建。";
    $("#chat-input").value = example;
    sendChat(example);
  };

  $("#btn-inspect").onclick = inspectSku;
  $("#sku-input").addEventListener("keydown", (e) => { if (e.key === "Enter") inspectSku(); });
  $("#btn-patrol").onclick = runPatrol;
  $("#btn-patrol-cancel").onclick = cancelPatrol;
  $("#btn-rag").onclick = ragSearch;
  $("#rag-input").addEventListener("keydown", (e) => { if (e.key === "Enter") ragSearch(); });
  $("#btn-sku-search").onclick = searchSkus;
  $("#sku-search").addEventListener("keydown", (e) => { if (e.key === "Enter") searchSkus(); });
}

// ================================================================
// 以下为「四层记忆 / 可观测性」新增前端能力：
//   · 定时任务状态徽章
//   · Query 重写（结构化计划）卡片 —— 蓝标归属
//   · 发送框常驻「当前事实卡」—— 黄标归属
//   · 每轮运行详情（证据链 + 5 段面板）—— 绿标归属
//   · 历史会话回放 / 记忆面板 / 版本自检 / 全局事件委托
// ================================================================

// ---------------------------------------------------------------- 定时任务状态徽章
function renderSchedulerBadge(health) {
  const badge = $("#scheduler-badge");
  if (!badge) return;
  const enabled = Boolean(health && health.scheduler_enabled);
  const next = health && health.scheduler_next_run;
  badge.className = "badge " + (enabled ? "badge-warn" : "badge-ok");
  badge.textContent = enabled
    ? `定时巡检：已开启${next ? "（下次 " + next.replace("T", " ").slice(5, 16) + "）" : ""}`
    : "定时巡检：已关闭（Demo 默认）";
  badge.title = (health && health.scheduler_note) ||
    "定时任务会真实调用 LLM；手动全量巡检不受影响";
}

// ---------------------------------------------------------------- Query 重写（结构化计划）
const QUERY_PLAN_KEY = "skuAgent.queryPlan";

// ---------------------------------------------------------------- 功能介绍（按勾选动态显示）
const FEATURE_HINT_TEXT = {
  query: {
    badge: "badge-plan",
    label: "Query 重写",
    text: "提问下方显示结构化查询计划（意图 / 实体 / 时间窗 / 必需数据 / 置信度 / plan_id），只展示、不改写原话。",
  },
  runDetail: {
    badge: "badge-run",
    label: "显示运行详情",
    text: "回答下方显示证据链 + 运行详情面板（工具时间线 / 事实卡+工作集 / token 与耗时 / 引用与权限）。",
  },
};

// 勾选哪个功能就显示哪行介绍；都没勾则不显示（容器清空并隐藏）
function renderFeatureHints() {
  const box = $("#feature-hints");
  if (!box) return;
  const enabled = [];
  const queryBox = $("#chk-query-plan");
  const runBox = $("#chk-run-detail");
  if (queryBox && queryBox.checked) enabled.push(FEATURE_HINT_TEXT.query);
  if (runBox && runBox.checked) enabled.push(FEATURE_HINT_TEXT.runDetail);
  box.innerHTML = "";
  if (!enabled.length) {
    box.classList.add("hidden");
    return;
  }
  enabled.forEach((item) => {
    const row = document.createElement("div");
    row.className = "feature-hint";
    const badge = document.createElement("span");
    badge.className = "artifact-badge " + item.badge;
    badge.textContent = item.label;
    const text = document.createElement("span");
    text.textContent = item.text;
    row.appendChild(badge);
    row.appendChild(text);
    box.appendChild(row);
  });
  box.classList.remove("hidden");
}

function initQueryPlanToggle() {
  const box = $("#chk-query-plan");
  if (!box) return;
  chatState.queryPlan = localStorage.getItem(QUERY_PLAN_KEY) === "1";
  box.checked = chatState.queryPlan;
  const label = box.closest ? box.closest(".chat-opt") : null;
  const sync = () => { if (label) label.classList.toggle("active-plan", box.checked); };
  sync();
  box.addEventListener("change", () => {
    chatState.queryPlan = box.checked;
    localStorage.setItem(QUERY_PLAN_KEY, box.checked ? "1" : "0");
    sync();
    renderFeatureHints();
  });
}

function planRow(label, value) {
  const row = document.createElement("div");
  row.className = "plan-row";
  const key = document.createElement("span");
  key.className = "plan-key";
  key.textContent = label;
  const val = document.createElement("span");
  val.className = "plan-val";
  val.textContent = value || "-";
  row.appendChild(key);
  row.appendChild(val);
  return row;
}

// 只渲染「当次提问」的计划；产物归属 = 蓝标「Query 重写」开关
function appendQueryPlanCard(plan, clarify) {
  if (!plan) return;
  const box = $("#chat-messages");
  const entities = plan.entities || {};
  const timeRange = plan.time_range || {};
  const details = document.createElement("details");
  details.className = "query-plan-card";
  details.open = true;

  const summary = document.createElement("summary");
  const badge = document.createElement("span");
  badge.className = "artifact-badge badge-plan";
  badge.textContent = "Query 重写";
  const pct = Math.round((plan.confidence || 0) * 100);
  const title = document.createElement("span");
  title.textContent = `针对上方问题 · ${plan.intent || "-"} · 置信度 ${pct}%`;
  summary.appendChild(badge);
  summary.appendChild(title);
  details.appendChild(summary);

  details.appendChild(planRow("意图", plan.intent));
  details.appendChild(planRow("SKU", (entities.skus || []).join(", ")));
  details.appendChild(planRow("款号", (entities.styles || []).join(", ")));
  details.appendChild(planRow("尺码/颜色", `${(entities.sizes || []).join(",") || "-"} / ${(entities.colors || []).join(",") || "-"}`));
  details.appendChild(planRow("时间窗", timeRange.label || "默认近 7 天"));
  details.appendChild(planRow("必需数据", (plan.required_data || []).join(", ")));
  details.appendChild(planRow("检索关键词", ((plan.retrieval_query || {}).keywords || []).slice(0, 10).join(", ")));
  details.appendChild(planRow("假设", (plan.assumptions || []).join("；")));
  details.appendChild(planRow("plan_id", plan.plan_id));
  details.appendChild(planRow("口径/时间", `${plan.metric_version || "-"} · ${plan.as_of || "-"}`));
  if (plan.generative_rewrite && (plan.generative_rewrite.rewrite || plan.generative_rewrite.error)) {
    details.appendChild(planRow("生成式改写", plan.generative_rewrite.rewrite || plan.generative_rewrite.error));
  }
  if (!plan.confidence || plan.confidence < 0.7) {
    details.classList.add("low-confidence");
    const note = document.createElement("div");
    note.className = "plan-note";
    note.textContent = "置信度偏低：计划里已标注假设，必要时补充 SKU 或时间窗可提高准确度。";
    details.appendChild(note);
  }
  if (clarify && clarify.question) {
    const ask = document.createElement("div");
    ask.className = "plan-ask";
    ask.textContent = "需要补充：" + clarify.question + (clarify.options && clarify.options.length
      ? `（可选：${clarify.options.join(" / ")}）` : "");
    details.appendChild(ask);
  }
  const foot = document.createElement("div");
  foot.className = "artifact-foot artifact-foot-plan";
  foot.textContent = "↑ 由「Query 重写」开关生成：仅展示结构化计划，不改写你的原话；回答里的运行详情见下方绿标区块。";
  details.appendChild(foot);

  box.appendChild(details);
  box.scrollTop = box.scrollHeight;
}

// ---------------------------------------------------------------- 发送框下方：当前事实卡（L1）
function setFactsUnderbar({ card, meta, empty }) {
  const body = $("#facts-underbar-body");
  const metaEl = $("#facts-underbar-meta");
  if (!body || !metaEl) return;
  if (card) {
    body.textContent = card;
    body.classList.remove("empty");
    metaEl.textContent = meta || "";
  } else {
    body.textContent = empty || "—";
    body.classList.add("empty");
    metaEl.textContent = meta || "";
  }
}

function updateFactsUnderbarFromDetail(detail) {
  const mem = (detail && detail.memory) || {};
  const context = mem.context || {};
  const card = context.facts_card || "";
  const version = context.facts_version || 0;
  const working = context.working_version || 0;
  const delta = mem.facts_delta || 0;
  const trigger = context.trigger || "-";
  const asOf = (detail && detail.as_of) || "";
  setFactsUnderbar({
    card,
    meta: card
      ? `事实卡 v${version}${delta ? `（本轮 +${delta}）` : "（本轮未变化）"} · 工作集 v${working} · 触发 ${trigger} · ${asOf}`
      : `事实卡 v${version} · 工作集 v${working} · 第 1 轮还没有可压缩的历史，第 2 轮起自动生成`,
    empty: "（本轮尚无事实卡：第 1 轮没有可压缩的历史；从第 2 轮起自动生成，超过阈值后早期原文会被替换成这张卡片）",
  });
}

async function refreshFactsUnderbar() {
  if (!state.token || !chatState.sessionId) {
    setFactsUnderbar({
      card: "",
      meta: "登录并发起一轮对话后，这里会显示当前会话的事实卡",
      empty: "—",
    });
    await refreshWorkingSetUnderbar();
    return;
  }
  try {
    const data = await api(`/api/memory/sessions/${chatState.sessionId}`);
    const card = data.facts_card || "";
    setFactsUnderbar({
      card,
      meta: card
        ? `事实卡 v${data.facts_version || 0} · 工作集 v${data.working_version || 0} · 触发 ${data.facts_trigger || "-"} · ${(data.facts_as_of || "").replace("T", " ")}`
        : `事实卡 v${data.facts_version || 0} · 工作集 v${data.working_version || 0} · 第 1 轮还没有可压缩的历史，第 2 轮起自动生成`,
      empty: "（本轮尚无事实卡：第 1 轮没有可压缩的历史；从第 2 轮起自动生成）",
    });
    setWorkingSetUnderbarFromApi(data);
  } catch (e) {
    setFactsUnderbar({ card: "", meta: "事实卡读取失败（可点「完整记忆」重试）", empty: "—" });
    await refreshWorkingSetUnderbar();
  }
}

// ---------------------------------------------------------------- 实体表（工作集）常驻区
// 事实卡记「结论」，实体表记「对象」：两块都常驻在发送框下方，一黄一紫。
const WORKING_SET_LABELS = {
  skus: "SKU", styles: "款号", sizes: "尺码",
  colors: "颜色", categories: "品类", metrics: "关注指标",
};
const WORKING_SET_EMPTY = "（本会话工作集为空：提问里出现 SKU / 款号 / 尺码 / 颜色 / 品类后会自动累积）";

function workingSetRowsFromDict(ws) {
  const rows = [];
  Object.entries(WORKING_SET_LABELS).forEach(([key, label]) => {
    const bucket = (ws && ws[key]) || {};
    const names = Object.keys(bucket);
    if (!names.length) return;
    const text = names.slice(0, 12).map((name) => {
      const hits = Number((bucket[name] || {}).hits || 0);
      return hits > 1 ? `${name} ×${hits}` : name;
    }).join(", ");
    rows.push([label, text]);
  });
  const tr = (ws && ws.time_range) || {};
  if (tr.label) rows.push(["时间窗", tr.label]);
  return rows;
}

// 老会话的 run_detail 里只有 <working-set> 文本，没有 dict：按文本行还原表格。
function workingSetRowsFromText(text) {
  const rows = [];
  String(text || "").split("\n").forEach((line) => {
    const t = (line || "").trim();
    if (!t || t.startsWith("<")) return;
    const idx = t.indexOf(":");
    if (idx <= 0) return;
    rows.push([t.slice(0, idx).trim(), t.slice(idx + 1).trim()]);
  });
  return rows;
}

function setWorkingSetUnderbar({ rows, text, meta, empty }) {
  const body = $("#working-set-underbar-body");
  const metaEl = $("#working-set-underbar-meta");
  if (!body || !metaEl) return;
  metaEl.textContent = meta || "";
  const copyBtn = $("#working-set-copy");
  body.innerHTML = "";
  if (rows && rows.length) {
    body.classList.remove("empty");
    const table = document.createElement("table");
    table.className = "working-set-table";
    const thead = document.createElement("thead");
    const headTr = document.createElement("tr");
    ["类型", "取值（命中次数）"].forEach((label) => {
      const th = document.createElement("th");
      th.textContent = label;
      headTr.appendChild(th);
    });
    thead.appendChild(headTr);
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    rows.forEach(([key, value]) => {
      const tr = document.createElement("tr");
      const nameCell = document.createElement("td");
      nameCell.textContent = key;
      const valueCell = document.createElement("td");
      valueCell.className = "mono small";
      valueCell.textContent = value;
      tr.appendChild(nameCell);
      tr.appendChild(valueCell);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    body.appendChild(table);
    if (copyBtn) {
      copyBtn.dataset.text = text || rows.map(([k, v]) => `${k}: ${v}`).join("\n");
    }
  } else {
    body.classList.add("empty");
    body.textContent = empty || WORKING_SET_EMPTY;
    if (copyBtn) copyBtn.dataset.text = text || "";
  }
}

function setWorkingSetUnderbarFromApi(data) {
  const rows = workingSetRowsFromDict((data && data.working_set) || {});
  const text = (data && data.working_set_text) || "";
  const updated = String((data && data.updated_at) || "").replace("T", " ");
  setWorkingSetUnderbar({
    rows: rows.length ? rows : workingSetRowsFromText(text),
    text,
    meta: `工作集 v${(data && data.working_version) || 0}`
      + `${updated ? " · 更新 " + updated : ""}`
      + ` · ${rows.length ? rows.length + " 类实体" : "暂无实体"}`,
    empty: WORKING_SET_EMPTY,
  });
}

function updateWorkingSetUnderbarFromDetail(detail) {
  const mem = (detail && detail.memory) || {};
  const context = mem.context || {};
  const text = context.working_set || "";
  const rows = workingSetRowsFromText(text);
  setWorkingSetUnderbar({
    rows,
    text,
    meta: `工作集 v${context.working_version || 0}`
      + ` · 本轮提问已并入（${rows.length ? rows.length + " 类实体" : "暂无实体"}）`,
    empty: WORKING_SET_EMPTY,
  });
}

async function refreshWorkingSetUnderbar() {
  if (!state.token || !chatState.sessionId) {
    setWorkingSetUnderbar({
      rows: [], text: "", meta: "登录并发起一轮对话后显示本会话实体表", empty: "—",
    });
    return;
  }
  try {
    const data = await api(`/api/memory/sessions/${chatState.sessionId}`);
    setWorkingSetUnderbarFromApi(data);
  } catch (e) {
    setWorkingSetUnderbar({ rows: [], text: "", meta: "实体表读取失败（可点「完整记忆」重试）", empty: "—" });
  }
}

function appendCitedNotice(ids) {
  if (!ids || !ids.length) return;
  const box = $("#chat-messages");
  const note = document.createElement("div");
  note.className = "chat-msg system";
  note.textContent = `本次回答引用的取数结果：${ids.join(", ")}（可回溯 result_id 与 as_of）`;
  box.appendChild(note);
  box.scrollTop = box.scrollHeight;
}

// ---------------------------------------------------------------- 本轮运行详情（可观测性）
const RUN_DETAIL_KEY = "skuAgent.showRunDetail";

function initRunDetailToggle() {
  const box = $("#chk-run-detail");
  if (!box) return;
  const saved = localStorage.getItem(RUN_DETAIL_KEY);
  box.checked = saved === null ? true : saved === "1";
  const label = box.closest ? box.closest(".chat-opt") : null;
  const sync = () => {
    if (label) label.classList.toggle("active-run", box.checked);
    document.querySelectorAll(".run-detail-wrap").forEach((el) => {
      el.classList.toggle("hidden", !box.checked);
    });
  };
  sync();
  box.addEventListener("change", () => {
    localStorage.setItem(RUN_DETAIL_KEY, box.checked ? "1" : "0");
    sync();
    renderFeatureHints();
  });
}

function runDetailEnabled() {
  const box = $("#chk-run-detail");
  return box ? box.checked : true;
}

function fmtMs(ms) {
  const n = Number(ms || 0);
  if (!n) return "-";
  return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${n}ms`;
}

function evidenceLine(detail) {
  const parts = [];
  // 多 Agent：先标出本轮由谁执行（agent@version），再列计划/取数/记忆/耗时
  const agent = detail.agent || {};
  if (agent.id) {
    const routed = agent.routed_by ? `, ${agent.routed_by}` : "";
    const ver = agent.version ? `@${agent.version}` : "";
    parts.push(`agent:${agent.id}${ver}${routed}`);
  }
  const plan = detail.plan || {};
  if (plan.plan_id) {
    parts.push(`${plan.plan_id} · ${plan.intent || "-"} · 置信度 ${Math.round((plan.confidence || 0) * 100)}%`);
  }
  (detail.steps || []).forEach((s) => {
    const tail = [s.result_id, s.as_of].filter(Boolean).join(", ");
    parts.push(`${s.tool}(${tail || "无 result_id"})${s.truncated ? "［截断→artifact］" : ""}`);
  });
  const mem = detail.memory || {};
  parts.push(`事实卡 v${mem.facts_version || 0}`);
  const llm = detail.llm || {};
  const tokens = (llm.input_tokens || 0) + (llm.output_tokens || 0);
  const phases = detail.phases || {};
  const split = (phases.llm_ms || phases.tools_ms)
    ? `（模型 ${fmtMs(phases.llm_ms || 0)} / 工具 ${fmtMs(phases.tools_ms || 0)}）`
    : "";
  parts.push(`耗时 ${fmtMs(detail.elapsed_ms)}${split} / ${fmtTokens(tokens)} tok`);
  return parts.join("  →  ");
}

function section(title, bodyEl) {
  const wrap = document.createElement("div");
  wrap.className = "run-section";
  const h = document.createElement("div");
  h.className = "run-section-title";
  h.textContent = title;
  wrap.appendChild(h);
  wrap.appendChild(bodyEl);
  return wrap;
}

function kvTable(rows) {
  const box = document.createElement("div");
  box.className = "kv-grid";
  rows.forEach(([k, v]) => {
    if (v === undefined || v === null || v === "") return;
    const cell = document.createElement("div");
    cell.className = "kv-cell";
    cell.innerHTML = `<span class="kv-key">${esc(k)}</span><span class="kv-val">${esc(String(v))}</span>`;
    box.appendChild(cell);
  });
  return box;
}

function fmtOffset(ms) {
  const n = Number(ms || 0);
  return `+${(n / 1000).toFixed(2)}s`;
}

function phasesText(detail) {
  const phases = detail.phases || {};
  const hasData = phases.llm_ms || phases.tools_ms || phases.other_ms || phases.total_ms;
  if (!hasData) return "";   // 旧版本记录没有耗时构成：不要用"其它 12.5s"这种假数据误导
  const total = detail.elapsed_ms || phases.total_ms || 0;
  const llmMs = phases.llm_ms || 0;
  const toolsMs = phases.tools_ms || 0;
  const other = phases.other_ms != null ? phases.other_ms : Math.max(0, total - llmMs - toolsMs);
  const llmCalls = (phases.llm_calls != null) ? phases.llm_calls : ((detail.llm && detail.llm.calls) || 0);
  const toolCalls = (phases.tool_calls != null) ? phases.tool_calls : (detail.steps || []).length;
  return `模型 ${fmtMs(llmMs)}（${llmCalls} 次） · 工具 ${fmtMs(toolsMs)}（${toolCalls} 次） · 其它 ${fmtMs(other)} · 整轮 ${fmtMs(total)}`;
}

function timelineSection(detail) {
  const events = detail.timeline || [];
  const box = document.createElement("div");
  const summary = phasesText(detail);
  if (summary) {
    const line = document.createElement("div");
    line.className = "muted small timeline-summary";
    line.textContent = summary;
    box.appendChild(line);
  }
  if (!events.length) {
    const tip = document.createElement("div");
    tip.className = "muted";
    tip.textContent = "本条记录没有时间轴（旧版本对话未采集模型调用耗时）；下方工具明细仍可回溯 result_id / as_of。";
    box.appendChild(tip);
    return box;
  }
  const rows = events.map((e) => {
    const kind = e.kind === "llm" ? "🧠 模型" : (e.kind === "tool" ? "🔧 工具" : "· 阶段");
    let meta = "";
    if (e.kind === "llm") {
      const tok = (e.input_tokens || e.output_tokens)
        ? `in ${fmtTokens(e.input_tokens || 0)} / out ${fmtTokens(e.output_tokens || 0)} tok` : "";
      meta = [e.kind_label, tok].filter(Boolean).join(" · ");
    } else if (e.kind === "tool") {
      meta = [e.result_id, e.as_of, e.agent_cached ? "缓存✓" : "",
              e.row_count ? `${e.row_count} 行` : "", e.truncated ? "截断→artifact" : ""]
        .filter(Boolean).join(" · ");
    }
    return {
      cls: e.kind === "llm" ? "row-llm" : (e.kind === "stage" ? "row-stage" : ""),
      cells: [
        { text: fmtOffset(e.offset_ms), cls: "mono small" },
        { text: kind },
        { text: e.label || "", cls: "mono small" },
        { text: e.kind === "stage" ? "-" : fmtMs(e.elapsed_ms) },
        { text: meta || "-", cls: "mono small" },
      ],
    };
  });
  box.appendChild(makeTable(["时刻", "类型", "事件", "耗时", "备注"], rows));
  return box;
}

function llmCallsTable(calls) {
  if (!calls || !calls.length) return null;
  const rows = calls.map((c) => ({
    cells: [
      { text: String(c.index || "") },
      { text: c.kind === "tool_call" ? "决定取数" : (c.kind === "answer" ? "生成答复" : "-") },
      { text: fmtMs(c.elapsed_ms) },
      { text: (c.input_tokens || c.output_tokens)
        ? `in ${fmtTokens(c.input_tokens || 0)} / out ${fmtTokens(c.output_tokens || 0)}` : "—",
        cls: "mono small" },
      { text: fmtOffset(c.start_offset_ms || 0), cls: "mono small" },
    ],
  }));
  return makeTable(["#", "用途", "耗时", "tokens", "开始于"], rows);
}

// 用真实 DOM 节点拼表格（而不是 innerHTML 字符串）：冒烟脚本的 DOM 垫片不解析 HTML，
// 只有节点化渲染才能被断言到，也避免行内容里的特殊字符被当成标签。
function makeTable(headers, rows) {
  const table = document.createElement("table");
  table.className = "run-table";
  const thead = document.createElement("thead");
  const headTr = document.createElement("tr");
  headers.forEach((label) => {
    const th = document.createElement("th");
    th.textContent = label;
    headTr.appendChild(th);
  });
  thead.appendChild(headTr);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    if (row.cls) tr.className = row.cls;
    (row.cells || []).forEach((cell) => {
      const td = document.createElement("td");
      if (cell.cls) td.className = cell.cls;
      td.textContent = cell.text == null ? "" : String(cell.text);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  const wrap = document.createElement("div");
  wrap.className = "table-scroll";
  wrap.appendChild(table);
  return wrap;
}

function toolTimeline(steps) {
  if (!steps || !steps.length) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "本轮未调用工具（可能直接作答或复用会话记忆中的 result_id）";
    return empty;
  }
  const wrap = document.createElement("div");
  wrap.className = "table-scroll";
  const rows = steps.map((s) => `
    <tr class="${s.status === "error" ? "row-error" : ""}">
      <td>${s.seq || ""}</td>
      <td class="mono">${esc(s.tool || "")}</td>
      <td class="mono small">${esc(s.args || "")}</td>
      <td>${esc(fmtMs(s.elapsed_ms))}</td>
      <td>${s.agent_cached ? '<span class="tag-hit">✓ 命中</span>' : "-"}</td>
      <td class="mono small">${esc(s.result_id || "-")}</td>
      <td class="mono small">${esc(s.as_of || "-")}</td>
      <td>${s.row_count ? esc(String(s.row_count)) : "-"}${s.truncated ? ' <span class="tag-warn">截断</span>' : ""}</td>
    </tr>`).join("");
  wrap.innerHTML = `
    <table class="run-table">
      <thead><tr><th>#</th><th>工具</th><th>入参</th><th>耗时</th><th>缓存</th><th>result_id</th><th>as_of</th><th>行数</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  return wrap;
}

function factsBlock(text, meta) {
  const box = document.createElement("div");
  box.className = "facts-block";
  const head = document.createElement("div");
  head.className = "facts-head";
  head.innerHTML = `<span>${esc(meta || "")}</span>`;
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "mini-btn secondary";
  btn.textContent = "复制";
  btn.onclick = () => copyText(text || "", btn);
  head.appendChild(btn);
  const pre = document.createElement("pre");
  pre.textContent = text || "（空）";
  box.appendChild(head);
  box.appendChild(pre);
  return box;
}

function copyText(text, btn) {
  const done = () => { if (btn) { btn.textContent = "已复制"; setTimeout(() => { btn.textContent = "复制"; }, 1200); } };
  try {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } catch (e) {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch (e) { /* 忽略 */ }
  ta.remove();
}

function memorySection(mem) {
  const box = document.createElement("div");
  const context = mem.context || {};
  const previous = mem.previous || {};
  const delta = mem.facts_delta || 0;
  box.appendChild(kvTable([
    ["事实卡版本", `v${context.facts_version || 0}` + (delta ? `（本轮 +${delta}）` : "（本轮未变化）")],
    ["工作集版本", `v${context.working_version || 0}`],
    ["触发原因", context.trigger || "-"],
    ["上下文规模", context.chars ? `${context.chars} 字` : "-"],
    ["上一轮", `事实卡 v${previous.facts_version || 0} / 工作集 v${previous.working_version || 0}`],
  ]));
  if (context.facts_card) {
    box.appendChild(factsBlock(context.facts_card, "本轮注入系统提示的事实卡（结论骨架，不含流水）"));
  } else {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.textContent = "本轮还没有事实卡：第 1 轮没有可压缩的历史，从第 2 轮起会自动生成"
      + "（历史超过阈值后，早期原文被替换成这张事实卡）。工作集这一轮已经生效，见下方。";
    box.appendChild(empty);
  }
  if (previous.facts_card && previous.facts_card !== context.facts_card) {
    box.appendChild(factsBlock(previous.facts_card, "上一轮结束时的事实卡（对比压缩变化）"));
  }
  if (context.working_set) {
    box.appendChild(factsBlock(context.working_set, "工作集实体表（<working-set>，用于解析『这个款/它』）"));
  }
  return box;
}

function refsSection(detail) {
  const box = document.createElement("div");
  const refs = detail.references || {};
  const perm = detail.permission || {};
  box.appendChild(kvTable([
    ["部门作用域", perm.department || "-"],
    ["允许范围", (perm.allowed || []).join(", ") || "全部"],
    ["失败/越权步骤", (perm.denied_steps || []).join(", ") || "无"],
    ["result_id", (refs.result_ids || []).join(", ") || "-"],
    ["artifact", (refs.artifacts || []).join(", ") || "-"],
    ["组织档案", (refs.analyses || []).join(", ") || "-"],
  ]));
  return box;
}

// 出口溯源自检（grounding）：状态 + 问题 + 引用/数字统计。
// 面试视角：这里把「数字为什么可信」变成可检查的字段，而不是一句「prompt 里要求了」。
const GROUNDING_LABELS = {
  passed: "通过（每个带单位的数字都能追溯到 result_id）",
  skipped: "跳过（本轮没有调用工具，无取数结果可对齐）",
  repaired: "已自动修正（重写一次后通过）",
  degraded: "未通过（重写后仍不达标，答案结尾已附告警）",
  disabled: "未启用（GROUNDING_ENABLED=false）",
  error: "校验器异常（不影响回答，仅此处不可用）",
};

function groundingPayload(detail) {
  return (detail && detail.grounding) || null;
}

function groundingNote(detail) {
  const g = groundingPayload(detail);
  if (!g) return null;
  const bad = g.status === "degraded" || g.status === "error";
  const fixed = g.status === "repaired";
  if (!bad && !fixed) return null;
  const issues = (g.issues || []).concat(g.after || []);
  const box = document.createElement("div");
  box.className = "grounding-note " + (bad ? "grounding-bad" : "grounding-fixed");
  box.textContent = bad
    ? `⚠️ 溯源自检未通过：${issues.slice(0, 2).join("；") || "数字未能完全对齐本轮取数结果"}`
    : `✅ 溯源自检：已自动补齐数字来源标注${issues.length ? `（原问题：${issues.slice(0, 1).join("")}）` : ""}`;
  return box;
}

function groundingSection(detail) {
  const box = document.createElement("div");
  const g = groundingPayload(detail);
  if (!g) {
    box.className = "muted";
    box.textContent = "本轮没有自检记录（旧版本对话或已关闭 grounding）。";
    return box;
  }
  const nums = g.numbers || {};
  const cites = g.citations || {};
  box.appendChild(kvTable([
    ["自检结论", GROUNDING_LABELS[g.status] || g.status],
    ["是否重写", g.rewritten ? "是（重写一次后复核）" : "否"],
    ["引用来源", `${cites.count || 0} 个 result_id`],
    ["虚构引用", (cites.unknown || []).join(", ") || "无"],
    ["带单位数字", `${nums.count || 0} 个`],
    ["未在工具结果中找到", (nums.unverified || []).slice(0, 8).join("、") || "无"],
    ["发现问题", (g.issues || []).join("；") || "无"],
    ["重写后问题", (g.after || []).join("；") || "无"],
    ["自检耗时", g.elapsed_ms != null ? fmtMs(g.elapsed_ms) : "-"],
  ]));
  return box;
}

function runDetailBadge(text) {
  const badge = document.createElement("span");
  badge.className = "artifact-badge badge-run";
  badge.textContent = text || "显示运行详情";
  return badge;
}

function attachRunDetail(bubble, detail, opts = {}) {
  if (!detail || !bubble) return;
  try {
    attachRunDetailInner(bubble, detail, opts);
  } catch (err) {
    const note = document.createElement("div");
    note.className = "run-detail-error";
    note.textContent = `运行详情渲染失败：${(err && err.message) || err}（回答不受影响）`;
    bubble.after(note);
  }
}

function attachRunDetailInner(bubble, detail, opts) {
  // 溯源告警条常显（不受「显示运行详情」开关影响：这是可信度信号，不是调试信息）
  const gnote = groundingNote(detail);
  if (gnote) bubble.after(gnote);
  // 产物归属 = 绿标「显示运行详情」；证据链常显，勾选只控制下面的面板
  const line = document.createElement("div");
  line.className = "evidence-line";
  line.appendChild(runDetailBadge());
  const lineText = document.createElement("span");
  lineText.className = "evidence-text";
  lineText.textContent = evidenceLine(detail);
  line.appendChild(lineText);
  bubble.after(line);

  const wrap = document.createElement("div");
  wrap.className = "run-detail-wrap" + (runDetailEnabled() ? "" : " hidden");
  line.after(wrap);

  const details = document.createElement("details");
  details.className = "run-detail";
  details.open = opts.open !== undefined ? Boolean(opts.open) : true;
  const summary = document.createElement("summary");
  const llm = detail.llm || {};
  const tokens = (llm.input_tokens || 0) + (llm.output_tokens || 0);
  const summaryText = document.createElement("span");
  summaryText.textContent = `运行详情（${(detail.steps || []).length} 次工具调用 · ${fmtMs(detail.elapsed_ms)} · ${fmtTokens(tokens)} tok）`;
  summary.appendChild(runDetailBadge());
  summary.appendChild(summaryText);
  details.appendChild(summary);

  const plan = detail.plan;
  const agentInfo = detail.agent || {};
  const agentRows = agentInfo.id ? [
    ["Agent", `${agentInfo.id}@${agentInfo.version || "-"}（路由：${agentInfo.routed_by || "-"}${agentInfo.fallback_from ? `，回退自 ${agentInfo.fallback_from}` : ""}）`],
    ["模型 / 参数", `${agentInfo.model_used || agentInfo.model || "-"}`
      + `${agentInfo.fallback_used ? `（回退自 ${agentInfo.model_requested || "原模型"}）` : ""}`
      + ` · 重试 ${agentInfo.retries || 0} 次 · 工具 ${agentInfo.tool_count ?? (agentInfo.tools || []).length}`
      + `${agentInfo.tools_all ? "（全部）" : ""} 个`
      + `${agentInfo.max_reflection_rounds ? ` · 反思轮上限 ${agentInfo.max_reflection_rounds}` : ""}`],
  ] : [];
  if (plan) {
    const sameSource = opts && opts.planCardShown;
    const body = kvTable([
      ...agentRows,
      ["意图", `${plan.intent || "-"}（置信度 ${Math.round((plan.confidence || 0) * 100)}%）`],
      ["SKU", (plan.entities && plan.entities.skus || []).join(", ") || "-"],
      ["款号/尺码", [(plan.entities && plan.entities.styles || []).join(","), (plan.entities && plan.entities.sizes || []).join(",")].filter(Boolean).join(" / ") || "-"],
      ["时间窗", (plan.time_range && plan.time_range.label) || "默认近 7 天"],
      ["必需数据", (plan.required_data || []).join(", ") || "-"],
      ["检索关键词", (plan.retrieval_keywords || []).join(", ") || "-"],
      ["指代回填", plan.resolved_from || "-"],
      ["假设", (plan.assumptions || []).join("；") || "-"],
      ["plan_id", plan.plan_id || "-"],
      ["口径版本", plan.metric_version || "-"],
    ]);
    if (sameSource) {
      const tip = document.createElement("div");
      tip.className = "artifact-foot artifact-foot-run";
      tip.textContent = `与上方蓝标「Query 重写」卡片是同一份计划（plan_id ${plan.plan_id || "-"} 相同）；`
        + "这里保留一份，是为了回放历史会话时（那时不显示蓝卡）仍能看清当时怎么理解的问题。";
      body.appendChild(tip);
    }
    details.appendChild(section("① 查询计划（query_plan）", body));
  } else {
    const noPlanBody = agentRows.length
      ? kvTable(agentRows)
      : Object.assign(document.createElement("div"), {
        className: "muted", textContent: "本轮未生成计划（QUERY_PLAN_ENABLED=false）",
      });
    details.appendChild(section("① 查询计划（query_plan）", noPlanBody));
  }

  details.appendChild(section("② 时间轴（阶段 / 模型调用 / 工具，按发生顺序）", timelineSection(detail)));
  details.appendChild(section("③ 工具调用明细（每次取数都可回溯）", toolTimeline(detail.steps)));
  details.appendChild(section("④ 记忆：事实卡 + 工作集", memorySection(detail.memory || {})));
  const callsTable = llmCallsTable(llm.calls_detail);
  const costBox = kvTable([
    ["模型", llm.model || "-"],
    ["LLM 调用", `${llm.calls || 0} 次` + (llm.errors ? `（失败 ${llm.errors}）` : "")],
    ["输入 tokens", (llm.input_tokens || 0).toLocaleString()],
    ["输出 tokens", (llm.output_tokens || 0).toLocaleString()],
    ["耗时构成", phasesText(detail)],
    ["采集时间", detail.as_of || "-"],
  ]);
  if (callsTable) costBox.appendChild(callsTable);
  details.appendChild(section("⑤ 模型与成本", costBox));
  details.appendChild(section("⑥ 溯源自检（数字 ↔ result_id）", groundingSection(detail)));
  details.appendChild(section("⑦ 引用与权限", refsSection(detail)));

  const foot = document.createElement("div");
  foot.className = "artifact-foot artifact-foot-run";
  foot.textContent = "↑ 由「显示运行详情」开关生成：证据链 + 时间轴（模型/工具耗时归因）+ 工具明细 + 记忆快照 + 成本 + 溯源自检 + 权限。";
  details.appendChild(foot);

  wrap.appendChild(details);
}

// ---------------------------------------------------------------- 历史会话 / 记忆面板
async function loadChatSessions() {
  const sel = $("#chat-history");
  if (!sel) return;
  if (!state.token) {
    sel.innerHTML = '<option value="">登录后可回放历史会话</option>';
    sel.disabled = true;
    return;
  }
  sel.disabled = false;
  try {
    const data = await api("/api/chat/sessions");
    const items = (data.items || []).filter((it) => (it.message_count === undefined ? true : it.message_count > 0));
    const current = chatState.sessionId || "";
    const hours = data.retention_hours || 24;
    if (!items.length) {
      sel.innerHTML = `<option value="">近 ${hours} 小时暂无历史会话</option>`;
      return;
    }
    sel.innerHTML = '<option value="">选择会话回放…</option>' + items.map((it) => {
      const title = (it.title || "新会话").slice(0, 14);
      const when = (it.updated_at || "").replace("T", " ").slice(5, 16);
      const cnt = it.message_count ? ` · ${it.message_count}条` : "";
      return `<option value="${esc(it.session_id)}"${it.session_id === current ? " selected" : ""}>${esc(title)}${cnt} · ${esc(when)}</option>`;
    }).join("");
    sel.title = `仅展示最近 ${hours} 小时的会话；更早的会话与消息会自动清理`;
  } catch (e) { /* 未登录/接口异常时保持原样 */ }
}

async function loadChatSession(sessionId) {
  if (!sessionId) return;
  const box = $("#chat-messages");
  const sel = $("#chat-history");
  const title = sel && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex].textContent : sessionId;
  box.innerHTML = '<div class="muted">正在加载历史会话（带回当时的运行详情与事实卡快照）…</div>';
  try {
    const data = await api(`/api/chat/${sessionId}`);
    box.innerHTML = "";
    const items = data.messages || [];
    if (!items.length) {
      box.innerHTML = '<div class="muted">该会话没有消息（旧版「新会话」按钮曾把消息清空；现在只会新开对话，不会再清空旧会话）。</div>';
      return;
    }
    const note = document.createElement("div");
    note.className = "chat-msg system";
    note.textContent = `已回放历史会话「${title}」共 ${items.length} 条消息：下方运行详情与事实卡是当时的快照；可直接继续提问（会接在这个会话后面）。`;
    box.appendChild(note);
    const assistants = items.filter((m) => m.role === "assistant");
    items.forEach((m) => {
      if (m.role === "user") { appendChat("user", m.content); return; }
      if (m.role !== "assistant") return;
      const bubble = appendChat("assistant", m.content, m.tool_trace || [],
                                { skipTrace: Boolean(m.run_detail) });
      const isLast = m === assistants[assistants.length - 1];
      attachRunDetail(bubble, m.run_detail, { open: isLast });
    });
    chatState.sessionId = sessionId;
    box.scrollTop = box.scrollHeight;
    const lastAssistant = assistants[assistants.length - 1];
    if (lastAssistant && lastAssistant.run_detail) {
      updateFactsUnderbarFromDetail(lastAssistant.run_detail);
      updateWorkingSetUnderbarFromDetail(lastAssistant.run_detail);
    } else {
      await refreshFactsUnderbar();
    }
  } catch (e) {
    box.innerHTML = `<div class="muted">加载失败：${esc(e.message || "未知错误")}</div>`;
  }
}

async function openMemoryPanel() {
  if (!state.token) { openLogin("请先登录账号查看会话记忆"); return; }
  const btn = $("#btn-memory");
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "读取中…"; }
  try {
    if (!chatState.sessionId) {
      await openMemorySessionPicker();
      return;
    }
    const data = await api(`/api/memory/sessions/${chatState.sessionId}`);
    const ws = data.working_set || {};
    const names = (key) => Object.keys(ws[key] || {}).join(", ") || "-";
    const rows = (data.recent_tool_results || []).map((r) => `
      <tr><td class="mono small">${esc(r.result_id)}</td><td class="mono small">${esc(r.tool)}</td>
      <td class="small">${esc(r.as_of)}</td><td>${r.truncated ? "截断→artifact" : "内联"}</td></tr>
    `).join("") || '<tr><td colspan="4" class="muted">暂无取数记录</td></tr>';
    const plan = data.latest_query_plan || {};
    openModal("记忆面板（本会话）", `
      <div class="kv-grid">
        <div class="kv-cell"><span class="kv-key">事实卡</span><span class="kv-val">v${data.facts_version || 0} · ${esc(data.facts_trigger || "-")} · ${esc((data.facts_as_of || "").replace("T", " "))}</span></div>
        <div class="kv-cell"><span class="kv-key">工作集</span><span class="kv-val">v${data.working_version || 0}</span></div>
        <div class="kv-cell"><span class="kv-key">已记录轮次</span><span class="kv-val">${data.turn_count || 0}</span></div>
        <div class="kv-cell"><span class="kv-key">最近计划</span><span class="kv-val">${esc(plan.plan_id || "-")} · ${esc(plan.intent || "-")}</span></div>
      </div>
      <div class="section-title" style="margin-top:12px">事实卡（&lt;analysis-facts&gt;）</div>
      <pre class="facts-pre">${esc(data.facts_card || "（暂无事实卡：第 1 轮还没有可压缩的历史，第 2 轮起自动生成）")}</pre>
      <div class="section-title" style="margin-top:12px">工作集实体（&lt;working-set&gt;）</div>
      <div class="muted">SKU：${esc(names("skus"))}<br/>款号：${esc(names("styles"))}<br/>尺码：${esc(names("sizes"))}<br/>品类：${esc(names("categories"))}<br/>指标：${esc(names("metrics"))}</div>
      <div class="section-title" style="margin-top:12px">最近取数台账（result_id）</div>
      <table class="breakdown-table"><thead><tr><th>result_id</th><th>工具</th><th>as_of</th><th>形态</th></tr></thead><tbody>${rows}</tbody></table>
    `);
  } catch (e) {
    openModal("记忆面板（本会话）", `<div class="issues">获取失败：${esc(e.message || "未知错误")}</div>`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

async function openMemorySessionPicker() {
  let items = [];
  try {
    const data = await api("/api/chat/sessions");
    items = data.items || [];
  } catch (e) { /* 忽略，走空态提示 */ }
  if (!items.length) {
    openModal("记忆面板（本会话）",
      '<div class="muted">还没有任何会话：先在下方发一句话（例如「ST-01-C1-M 当前可用库存」），事实卡与工作集就会出现在这里。</div>');
    return;
  }
  openModal("记忆面板（本会话）",
    '<div class="muted">当前没有进行中的会话。选一条最近的会话，查看它的事实卡 / 工作集 / 取数台账：</div>');
  const body = $("#modal-body");
  if (!body) return;
  const table = document.createElement("table");
  table.className = "breakdown-table";
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>会话</th><th>更新时间</th><th></th></tr>";
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  items.forEach((it) => {
    const tr = document.createElement("tr");
    const tdTitle = document.createElement("td");
    tdTitle.textContent = (it.title || "新会话").slice(0, 22);
    const tdTime = document.createElement("td");
    tdTime.className = "small";
    tdTime.textContent = (it.updated_at || "").replace("T", " ");
    const tdAction = document.createElement("td");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mini-btn";
    btn.textContent = "查看记忆";
    btn.onclick = async () => {
      await loadChatSession(it.session_id);
      closeModal();
      await openMemoryPanel();
    };
    tdAction.appendChild(btn);
    tr.appendChild(tdTitle);
    tr.appendChild(tdTime);
    tr.appendChild(tdAction);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  body.appendChild(table);
}

// ---------------------------------------------------------------- 实时进度卡（等待毫秒 + 当前步骤）
const STAGE_ORDER = ["plan", "memory", "agent", "tools", "answer"];
const STAGE_LABELS = {
  plan: "解析计划", memory: "记忆", agent: "模型", tools: "取数", answer: "答复",
};

function createProgressCard() {
  const card = document.createElement("div");
  card.className = "progress-card";
  const head = document.createElement("div");
  head.className = "progress-head";
  const dot = document.createElement("span");
  dot.className = "spinner";
  const time = document.createElement("span");
  time.className = "progress-time";
  time.textContent = "已等待 0.0s";
  const stage = document.createElement("span");
  stage.className = "progress-stage";
  stage.textContent = "准备中…";
  head.appendChild(dot);
  head.appendChild(time);
  head.appendChild(stage);
  card.appendChild(head);

  const chips = document.createElement("div");
  chips.className = "progress-stages";
  STAGE_ORDER.forEach((key) => {
    const chip = document.createElement("span");
    chip.className = "step-wait";
    chip.dataset.stage = key;
    chip.textContent = STAGE_LABELS[key];
    chips.appendChild(chip);
  });
  card.appendChild(chips);

  const steps = document.createElement("div");
  steps.className = "progress-steps";
  card.appendChild(steps);

  const startedAt = performance.now();
  // 当前阶段：文字 + 起始时刻，用来在等待时显示「模型调用 #3 · 已 3.1s」，
  // 否则模型思考的几秒里阶段会一直停在「取数：xxx」（看起来像工具在慢慢跑）。
  let stageLabel = "准备中…";
  let stageStart = performance.now();
  const renderStage = () => {
    const secs = (performance.now() - stageStart) / 1000;
    stage.textContent = secs >= 0.3 ? `${stageLabel} · 已 ${secs.toFixed(1)}s` : stageLabel;
  };
  renderStage();
  const timer = setInterval(() => {
    const ms = performance.now() - startedAt;
    time.textContent = `已等待 ${(ms / 1000).toFixed(1)}s`;
    renderStage();
  }, 100);

  const api = {
    el: card,
    remove() { clearInterval(timer); card.remove(); },
    setServerElapsed(ms) {
      if (typeof ms === "number" && ms >= 0) {
        time.textContent = `已等待 ${(ms / 1000).toFixed(1)}s`;
      }
    },
    setStage(key, label) {
      stageLabel = label || key;
      stageStart = performance.now();
      renderStage();
      chips.querySelectorAll("[data-stage]").forEach((chip) => {
        const k = chip.dataset.stage;
        if (k === key) chip.className = "step-run";
        else if (STAGE_ORDER.indexOf(k) < STAGE_ORDER.indexOf(key)) chip.className = "step-ok";
      });
      if (key === "agent") api.setStageToolsRunning();
    },
    setStageToolsRunning() {
      const chip = chips.querySelector('[data-stage="tools"]');
      if (chip && chip.className === "step-wait") chip.className = "step-run";
    },
    addStepStart(evt) {
      const row = document.createElement("div");
      row.className = "progress-step running";
      row.dataset.tool = evt.tool || "";
      row.dataset.seq = String(evt.seq || "");
      row.textContent = `⏳ 取数：${evt.tool || ""}${evt.args ? " " + evt.args : ""}`;
      steps.appendChild(row);
    },
    completeStep(evt) {
      const rows = Array.from(steps.querySelectorAll("[data-tool]"));
      const row = rows.reverse().find((r) => r.dataset.tool === (evt.tool || ""));
      if (!row) return;
      row.className = "progress-step " + (evt.status === "error" ? "error" : "ok");
      const tail = [evt.result_id, evt.as_of].filter(Boolean).join(" ");
      row.textContent = `${evt.status === "error" ? "✗" : "✓"} ${evt.tool} ${tail}`
        + `${evt.elapsed_ms ? " · " + fmtMs(evt.elapsed_ms) : ""}`
        + `${evt.truncated ? " · 截断→artifact" : ""}${evt.error ? " · " + evt.error : ""}`;
    },
    // 模型调用开始：阶段文字切成"模型调用 #k：推理中"，计时器随后每秒刷新
    addLlmStart(evt) {
      if (Number(evt.elapsed_ms) > 0) api.setServerElapsed(evt.elapsed_ms);
      api.setStage("agent", `模型调用 #${evt.call || "?"}：推理中`);
    },
    // 模型调用结束：在步骤区留一行，标明这次是"决定取数"还是"生成答复"以及 token
    addLlmEnd(evt) {
      if (Number(evt.elapsed_ms) > 0) api.setServerElapsed(evt.elapsed_ms);
      const row = document.createElement("div");
      row.className = "progress-step ok progress-llm";
      const tok = (evt.input_tokens || evt.output_tokens)
        ? ` · in ${fmtTokens(evt.input_tokens || 0)} / out ${fmtTokens(evt.output_tokens || 0)} tok`
        : "";
      const kind = evt.kind === "tool_call" ? "决定取数" : (evt.kind === "answer" ? "生成答复" : "");
      row.textContent = `🧠 模型 #${evt.call || "?"} 返回 ${fmtMs(evt.duration_ms)}${tok}`
        + `${kind ? " · " + kind : ""}`;
      steps.appendChild(row);
      if (evt.kind === "answer") {
        api.setStage("answer", "答复已生成，正在本地汇总");
      } else {
        api.setStage("tools", "取数中…");
      }
    },
    fail(message) {
      stageLabel = "失败：" + (message || "未知错误");
      stage.textContent = stageLabel;
      dot.className = "spinner err";
    },
  };
  return api;
}

function appendWarnNote(text) {
  const box = $("#chat-messages");
  const note = document.createElement("div");
  note.className = "chat-warn";
  note.textContent = text;
  box.appendChild(note);
  box.scrollTop = box.scrollHeight;
}

function chatPayload(message) {
  return {
    session_id: chatState.sessionId,
    message,
    include_query_plan: Boolean(chatState.queryPlan),
  };
}

async function postChat(message) {
  return await api("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(chatPayload(message)),
  });
}

// 优先走 SSE 流式（能边等边看步骤）；**只有传输层失败**才降级为普通 POST。
// 业务冲突（409/429/503…）不降级：重试只会再撞一次，还会白烧一轮模型。
async function requestChat(message, progress) {
  if (typeof fetch !== "function" || typeof ReadableStream === "undefined") {
    progress.setStage("agent", "浏览器不支持流式读取，已改用普通请求");
    return await postChat(message);
  }
  try {
    return await streamChat(message, progress);
  } catch (err) {
    const reason = (err && err.message) || String(err);
    if (err && err.noFallback) {
      // 把服务端的可读错误直接交给上层展示（例如「该会话正在处理上一轮，请稍候再发」）
      progress.setStage("agent", reason);
      throw err;
    }
    console.warn("[chat] 流式失败，降级 POST：", reason);
    progress.setStage("agent", `流式不可用（${reason}），已降级为普通请求`);
    return await postChat(message);
  }
}

// 统一鉴权头：普通请求与流式请求都要带 Bearer Token（之前 streamChat 漏了导致 401 降级）
function authHeaders(extra) {
  const headers = Object.assign({}, extra || {});
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  return headers;
}

async function streamChat(message, progress) {
  const resp = await fetch("/api/chat/stream", {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json", Accept: "text/event-stream" }),
    body: JSON.stringify(chatPayload(message)),
  });
  if (resp.status === 401) {
    clearToken();
    renderAccount();
    renderAnonymous();
    resetChatState("登录已失效，请重新登录账号后继续对话。");
    openLogin("登录已失效，请重新登录账号");
    throw new Error("HTTP 401 未登录/登录失效");
  }
  if (!resp.ok) {
    // 业务冲突类错误（409 同会话并发 / 429 频次 / 503 锁后端不可用）**不能**回退到 POST：
    // 回退等于原地重试，只会再撞一次同样的错，还白烧一轮上游模型。
    const payload = await resp.json().catch(() => null);
    const detail = (payload && (payload.detail || payload.message)) || "";
    const err = new Error(`HTTP ${resp.status}${detail ? "：" + String(detail).slice(0, 160) : ""}`);
    err.status = resp.status;
    err.noFallback = [400, 401, 409, 422, 429, 503].includes(resp.status);
    throw err;
  }
  if (!resp.body || !resp.body.getReader) throw new Error("响应不支持流式读取");

  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let finalData = null;
  let streamError = null;

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let event = "message";
      const dataLines = [];
      block.split("\n").forEach((line) => {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      });
      if (!dataLines.length) continue;
      let payload = {};
      try { payload = JSON.parse(dataLines.join("\n")); } catch (e) { continue; }
      handleStreamEvent(event, payload, progress);
      if (event === "done") finalData = payload;
      if (event === "error") streamError = payload;
    }
  }
  if (streamError) {
    const err = new Error(streamError.message || "未知错误");
    if (streamError.error_code) err.code = streamError.error_code;
    if (streamError.trace_id) err.trace = streamError.trace_id;
    // fatal：服务端判定「重发同一条请求没有意义」（工具循环 / 预算用尽），
    // 与「传输层抖动」区分开——不能自动降级 POST（降级等于把同一个循环再跑一遍）
    if (streamError.fatal) err.noFallback = true;
    throw err;
  }
  if (!finalData) throw new Error("流式响应未返回最终结果");
  return finalData;
}

function handleStreamEvent(event, payload, progress) {
  switch (event) {
    case "stage":
      progress.setServerElapsed(payload.elapsed_ms);
      if (payload.stage === "plan_done") {
        progress.setStage("plan", `计划已生成（${payload.intent || "-"}）`);
      } else {
        progress.setStage(payload.stage, payload.label || payload.stage);
      }
      break;
    case "delta":
      // 真·流式：模型边生成边推片段，先把气泡写出来（done 时会用最终答复覆盖，
      // 因为溯源校验可能重写答案，最终内容以 done 为准）
      if (payload.text) appendStreamDelta(String(payload.text));
      break;
    case "delta_retract":
      // 模型「先说话、后决定调工具」：那段文本不是最终答案，服务端已撤回（A4）
      clearStreamBubble();
      progress.setStage("retract", payload.chars ? `已撤回 ${payload.chars} 字过程文字` : "已撤回过程文字");
      break;
    case "tool_start":
      progress.setStage("tools", payload.label || `取数：${payload.tool}`);
      progress.addStepStart(payload);
      break;
    case "tool_end":
      progress.completeStep(payload);
      break;
    case "llm_start":
      progress.addLlmStart(payload);
      break;
    case "llm_end":
      progress.addLlmEnd(payload);
      break;
    case "tick":
      progress.setServerElapsed(payload.elapsed_ms);
      break;
    case "loop_aborted":
      // 工具循环守护（P1-1）：后端判定本轮陷入循环，马上就要推 error 事件。
      // 这里先把阶段文案改掉，避免用户盯着「取数中」等一个不会来的答复。
      progress.setStage("guard", `${payload.error_code || "工具循环"}：本轮已中止`);
      break;
    case "error":
      progress.fail(payload.message);
      break;
    default:
      break;
  }
}

// ---------------------------------------------------------------- 自检面板（定位"点了没反应"）
async function openDiagnostics() {
  const pageBuild = (document.body && document.body.dataset.uiBuild) || "";
  const jsBuild = window.__UI_BUILD || "";
  const ls = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  let health = {};
  try { health = await api("/api/health"); } catch (e) { health = { error: String(e.message || e) }; }
  const rows = [
    ["页面 build", pageBuild || "-"],
    ["脚本 build", jsBuild || "缺失（旧脚本）"],
    ["版本一致", pageBuild && jsBuild ? (pageBuild === jsBuild ? "✔ 一致" : "✘ 不一致") : "无法判断"],
    ["服务器 assets", health.assets ? `app.js=${health.assets["app.js"]} / style.css=${health.assets["style.css"]}` : "-"],
    ["服务器 features", (health.features || []).join(", ") || "-"],
    ["检索栈", health.vector_store
      ? `向量库=${health.vector_store.backend}${health.vector_store.points != null ? `(${health.vector_store.points})` : ""}${health.vector_store.degraded ? "·降级" : ""} / Embedding=${(health.embedding || {}).backend || "-"}${(health.embedding || {}).degraded ? "·降级" : ""}`
      : "-"],
    ["attachRunDetail", typeof attachRunDetail],
    ["appendQueryPlanCard", typeof appendQueryPlanCard],
    ["openMemoryPanel", typeof openMemoryPanel],
    ["chat_stream(前端)", typeof streamChat],
    ["localStorage 开关", `Query重写=${ls("skuAgent.queryPlan") ?? "未设置"} / 运行详情=${ls("skuAgent.showRunDetail") ?? "未设置"}`],
    ["checkbox 状态", `Query重写=${$("#chk-query-plan") ? $("#chk-query-plan").checked : "缺失"} / 运行详情=${$("#chk-run-detail") ? $("#chk-run-detail").checked : "缺失"}`],
    ["DOM 计数", `计划卡=${document.querySelectorAll(".query-plan-card").length} / 证据链=${document.querySelectorAll(".evidence-line").length} / 详情面板=${document.querySelectorAll(".run-detail-wrap").length} / 事实卡块=${document.querySelectorAll(".facts-block").length} / 实体表=${document.querySelectorAll(".working-set-table").length}`],
    ["事实卡常驻区", ($("#facts-underbar-body") ? String($("#facts-underbar-body").textContent || "").slice(0, 40) : "缺失")],
    ["实体表常驻区", ($("#working-set-underbar-body") ? String($("#working-set-underbar-body").textContent || "").slice(0, 40) : "缺失")],
    ["当前会话", chatState.sessionId || "（无）"],
    ["最近一次对话", LAST_CHAT_DEBUG ? JSON.stringify(LAST_CHAT_DEBUG) : "（本次页面尚未对话）"],
  ];
  const table = rows.map(([k, v]) => `<tr><td>${esc(k)}</td><td class="mono small">${esc(String(v))}</td></tr>`).join("");
  const errors = UI_ERRORS.length
    ? UI_ERRORS.slice(-5).map((e) => `<li class="mono small">${esc(e)}</li>`).join("")
    : '<li class="muted">无</li>';
  openModal("前端自检", `
    <div class="muted">用于定位「勾选了却没出现」类问题：先看版本是否一致，再看最近一次对话响应是否带 run_detail/query_plan。内容较多时本窗口内可上下滚动。</div>
    <div class="table-scroll">
      <table class="breakdown-table"><thead><tr><th>检查项</th><th>值</th></tr></thead><tbody>${table}</tbody></table>
    </div>
    <div class="section-title" style="margin-top:10px">最近前端错误</div>
    <ul class="diag-errors">${errors}</ul>
    <div class="diag-actions">
      <button type="button" class="mini-btn" id="diag-probe">探测后端一次对话</button>
      <button type="button" class="mini-btn secondary" id="diag-reset">重置本地开关并强刷</button>
      <button type="button" class="mini-btn secondary" id="diag-reload">强刷页面</button>
    </div>
  `);
  const probe = $("#diag-probe");
  if (probe) {
    probe.onclick = async () => {
      probe.disabled = true;
      probe.textContent = "探测中…";
      try {
        const data = await postChat("ST-01-C1-M 当前可用库存");
        const info = {
          keys: Object.keys(data || {}),
          hasRunDetail: Boolean(data.run_detail),
          hasQueryPlan: Boolean(data.query_plan),
          session_id: data.session_id,
        };
        LAST_CHAT_DEBUG = { at: new Date().toLocaleTimeString(), ...info };
        openModal("探测结果", `<div class="mono small">${esc(JSON.stringify(info, null, 2))}</div>`);
      } catch (e) {
        openModal("探测失败", `<div class="issues">${esc((e && e.message) || "未知错误")}</div>`);
      } finally {
        probe.disabled = false;
        probe.textContent = "探测后端一次对话";
      }
    };
  }
  const resetBtn = $("#diag-reset");
  if (resetBtn) {
    resetBtn.onclick = () => {
      try {
        localStorage.removeItem("skuAgent.queryPlan");
        localStorage.removeItem("skuAgent.showRunDetail");
      } catch (e) { /* 忽略 */ }
      location.replace(location.pathname + "?v=" + Date.now());
    };
  }
  const reloadBtn = $("#diag-reload");
  if (reloadBtn) reloadBtn.onclick = () => location.replace(location.pathname + "?v=" + Date.now());
}

// ---------------------------------------------------------------- 延迟分位（P50/P95）
function fmtMsValue(ms) {
  const n = Number(ms || 0);
  if (!n) return "-";
  return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${Math.round(n)}ms`;
}

function renderLatencyCard(latency) {
  const p50 = $("#m-lat-p50");
  const p95 = $("#m-lat-p95");
  if (!p50 || !p95) return;
  const overall = (latency && latency.overall) || {};
  p50.textContent = fmtMsValue(overall.p50);
  p95.textContent = fmtMsValue(overall.p95);
  const card = $("#card-hit");
  if (card) {
    card.title = `延迟分位（${(latency && latency.note) || "进程内采样"}）\n`
      + Object.entries((latency && latency.categories) || {}).map(([k, v]) =>
        `${(latency.labels || {})[k] || k}：P50 ${fmtMsValue(v.p50)} / P95 ${fmtMsValue(v.p95)}（${v.count} 次）`).join("\n");
  }
}

function openLatencyBreakdown() {
  const m = state.metrics;
  const latency = (m && m.latency) || {};
  const cats = latency.categories || {};
  const labels = latency.labels || {};
  const rows = Object.entries(cats).map(([k, v]) => `
    <tr><td>${esc(labels[k] || k)}</td><td>${v.count || 0}</td>
    <td>${esc(fmtMsValue(v.p50))}</td><td>${esc(fmtMsValue(v.p95))}</td>
    <td>${esc(fmtMsValue(v.p99))}</td><td>${esc(fmtMsValue(v.max))}</td></tr>`).join("")
    || '<tr><td colspan="6" class="muted">暂无采样</td></tr>';
  const overall = latency.overall || {};
  openModal("缓存 & 延迟明细", `
    <div class="section-title">① 缓存命中（Agent 旁路缓存 + 数据中台）</div>
    <div id="cache-breakdown" class="muted">正在读取缓存明细…</div>
    <div class="section-title" style="margin-top:14px">② 延迟分位（P50 / P95 / P99）</div>
    <div class="breakdown-total">整体：P50 ${esc(fmtMsValue(overall.p50))} · P95 ${esc(fmtMsValue(overall.p95))} · P99 ${esc(fmtMsValue(overall.p99))}（${overall.count || 0} 次采样）</div>
    <table class="breakdown-table">
      <thead><tr><th>分类</th><th>次数</th><th>P50</th><th>P95</th><th>P99</th><th>最大</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="breakdown-total">${esc(latency.note || "进程内最近 1000 次采样，重启清零")}；<code>接口耗时</code> 不含每 3 秒轮询的 /api/metrics、/api/health。</div>
    <div class="section-title" style="margin-top:14px">③ 工具调用可靠性（成功 / 重试 / 超时 / P95）</div>
    <div id="tool-stats" class="muted">${esc(toolStatsNote(m.tools))}</div>
    ${toolStatsTable(m.tools)}
  `);
  loadCacheBreakdown();
}

function toolStatsNote(tools) {
  const overall = (tools && tools.overall) || {};
  if (!overall.calls) return "暂无工具调用采样（对话或巡检跑一轮后出现）";
  const oc = overall.outcomes || {};
  const bad = (oc.business_error || 0) + (oc.transport_error || 0)
    + (oc.timeout || 0) + (oc.breaker_open || 0);
  const detail = bad
    ? ` · 失败拆分：业务 ${oc.business_error || 0} / 链路 ${oc.transport_error || 0}`
      + ` / 超时 ${oc.timeout || 0} / 熔断 ${oc.breaker_open || 0}`
    : "";
  return `整体：调用 ${overall.calls} 次 · 成功率 ${((overall.success_rate || 0) * 100).toFixed(0)}%`
    + ` · 重试 ${overall.retries} · 超时 ${overall.timeouts}${detail}`;
}

function outcomesText(v) {
  const oc = (v && v.outcomes) || {};
  const parts = [];
  if (oc.business_error) parts.push(`业务 ${oc.business_error}`);
  if (oc.transport_error) parts.push(`链路 ${oc.transport_error}`);
  if (oc.timeout) parts.push(`超时 ${oc.timeout}`);
  if (oc.breaker_open) parts.push(`熔断 ${oc.breaker_open}`);
  return parts.join(" / ") || "-";
}

function toolStatsTable(tools) {
  const rows = Object.entries((tools && tools.tools) || {}).map(([name, v]) => `
    <tr>
      <td class="mono">${esc(name)}</td>
      <td>${v.calls || 0}</td>
      <td>${((v.success_rate || 0) * 100).toFixed(0)}%</td>
      <td>${esc(outcomesText(v))}</td>
      <td>${esc(fmtMsValue(v.p95))}</td>
      <td>${v.retries || 0}</td>
      <td>${v.timeouts || 0}</td>
    </tr>`).join("");
  if (!rows) return "";
  return `<table class="breakdown-table">
    <thead><tr><th>工具（server.tool）</th><th>调用</th><th>成功率</th><th>失败分类</th><th>P95</th><th>重试</th><th>超时</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>
  <div class="muted" style="margin-top:6px">失败分类：业务错误（参数/权限，重试无用）、链路错误（中台/网络，可重试）、超时、熔断（快速失败）。</div>`;
}

async function loadCacheBreakdown() {
  const box = $("#cache-breakdown");
  if (!box) return;
  try {
    const data = await api("/api/cache/breakdown");
    const agent = data.agent || {};
    const platform = data.platform || null;
    const ttl = data.ttl || {};
    const pct = (rate) => `${(Number(rate || 0) * 100).toFixed(0)}%`;
    const agentLine = `命中 ${agent.hits || 0} / 未命中 ${agent.misses || 0} / 写入 ${agent.writes || 0} → 命中率 <b>${pct(agent.hit_rate)}</b>`;
    const platformLine = platform
      ? `命中 ${platform.hits || 0} / 未命中 ${platform.misses || 0} → 命中率 <b>${pct(platform.hit_rate)}</b>`
      : `<span class="warn-text">读取失败：${esc(data.platform_error || "不可用")}</span>`;
    box.innerHTML = `
      <div class="kv-grid">
        <div class="kv-cell"><span class="kv-key">Agent 旁路</span><span class="kv-val">${agentLine}</span></div>
        <div class="kv-cell"><span class="kv-key">数据中台</span><span class="kv-val">${platformLine}</span></div>
        <div class="kv-cell"><span class="kv-key">TTL</span><span class="kv-val">取数 ${ttl.platform || "-"}s · MCP ${ttl.mcp || "-"}s · RAG ${ttl.rag || "-"}s（带随机抖动）</span></div>
      </div>
      <div class="muted" style="margin-top:6px">${esc(data.note || "")}</div>
    `;
  } catch (e) {
    box.innerHTML = `<span class="warn-text">读取失败：${esc((e && e.message) || "未知错误")}</span>`;
  }
}

// ---------------------------------------------------------------- 版本自检 / 全局事件
function checkUiBuild() {
  const expected = (document.body && document.body.dataset.uiBuild) || "";
  const broken = typeof attachRunDetail !== "function" || typeof openMemoryPanel !== "function";
  const mismatch = Boolean(expected && expected !== UI_BUILD);
  if (!broken && !mismatch) return;
  const bar = document.createElement("div");
  bar.className = "stale-banner";
  bar.innerHTML = `⚠ 页面脚本与页面版本不一致（浏览器可能缓存了旧 <code>app.js</code>：` +
    `页面 ${esc(expected || "?")} / 脚本 ${esc(UI_BUILD)}）。` +
    `<button type="button" class="mini-btn" id="stale-reload">强制刷新</button>`;
  document.body.prepend(bar);
  const btn = bar.querySelector("#stale-reload");
  if (btn) {
    btn.onclick = () => {
      const url = new URL(location.href);
      url.searchParams.set("v", Date.now());
      location.replace(url.toString());
    };
  }
}

function bindGlobalHandlers() {
  document.addEventListener("click", (e) => {
    const target = e.target && e.target.closest ? e.target.closest("#btn-memory") : null;
    if (target) { e.preventDefault(); openMemoryPanel(); }
  });
  document.addEventListener("click", (e) => {
    const target = e.target && e.target.closest ? e.target.closest("#btn-diag") : null;
    if (target) { e.preventDefault(); openDiagnostics(); }
  });
  document.addEventListener("change", (e) => {
    if (e.target && e.target.id === "chat-history" && e.target.value) {
      loadChatSession(e.target.value);
    }
  });
  document.addEventListener("focusin", (e) => {
    if (e.target && e.target.id === "chat-history") loadChatSessions();
  });
  document.addEventListener("click", (e) => {
    const src = e.target;
    if (!src || !src.closest) return;
    if (src.closest("#facts-copy")) {
      e.preventDefault();
      const body = $("#facts-underbar-body");
      const text = body ? String(body.textContent || "") : "";
      if (!text || text === "—") return;
      copyText(text, src.closest("#facts-copy"));
      return;
    }
    if (src.closest("#facts-more")) {
      e.preventDefault();
      openMemoryPanel();
      return;
    }
    if (src.closest("#working-set-copy")) {
      e.preventDefault();
      const btn = src.closest("#working-set-copy");
      const text = (btn && btn.dataset && btn.dataset.text) || "";
      if (!text) return;
      copyText(text, btn);
      return;
    }
  });
  document.addEventListener("click", (e) => {
    const card = e.target && e.target.closest ? e.target.closest(".facts-underbar-body") : null;
    if (card) card.classList.toggle("expanded");
  });
  const tag = document.getElementById("build-tag");
  if (tag) tag.textContent = "UI " + UI_BUILD;
}

(async function init() {
  bindGlobalHandlers();
  try {
    bindEvents();
  } catch (err) {
    console.error("bindEvents 初始化失败：", err);
  }
  resetPlaceholders();
  renderAnonymous();
  checkUiBuild();
  await loadHealth();
  if (state.token) {
    try {
      const data = await api("/api/auth/me");
      state.user = data.user;
      renderAccount();
    } catch (e) {
      clearToken();
      renderAccount();
      renderAnonymous();
    }
  } else {
    renderAccount();
  }
  await refreshData();  // 只读数据：未登录也可正常展示
  await refreshFactsUnderbar();
  // 页面不可见时暂停轮询：避免常开 Tab 空转请求（演示环境降流量）
  setInterval(() => {
    if (document.hidden) return;
    loadMetrics().catch(() => {});
  }, 3000);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    loadMetrics().catch(() => {});
    loadHealth().catch(() => {});
  });
})();
