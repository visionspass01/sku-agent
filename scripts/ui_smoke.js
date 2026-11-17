/**
 * UI 冒烟测试（无浏览器）：用最小 DOM 垫片加载 app.js，实际执行
 * 「发送对话 → 渲染运行详情 → 切换勾选 → 打开记忆面板 → 回放历史会话」。
 *
 * 用法：node scripts/ui_smoke.js      （退出码 0 = 全通过；无 node 时 pytest 会跳过）
 * 目的：防止"HTML 是新的、app.js 是旧的"或渲染函数抛错这类问题再次静默发生。
 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const APP_JS = fs.readFileSync(path.join(ROOT, "web/app.js"), "utf8");
const INDEX_HTML = fs.readFileSync(path.join(ROOT, "web/index.html"), "utf8");

const failures = [];
const check = (name, cond, extra) => {
  if (cond) {
    console.log(`  ok   ${name}`);
  } else {
    console.log(`  FAIL ${name}${extra !== undefined ? " -> " + JSON.stringify(extra) : ""}`);
    failures.push(name);
  }
};

// ---------------------------------------------------------------- 最小 DOM
class ClassSet {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach((x) => this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  contains(c) { return this.set.has(c); }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : Boolean(force);
    if (on) this.set.add(c); else this.set.delete(c);
    return on;
  }
  toString() { return Array.from(this.set).join(" "); }
}

class El {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.parent = null;
    this._class = "";
    this.classList = new ClassSet();
    this._text = "";
    this._html = "";
    this.dataset = {};
    this.style = {};
    this.options = [];
    this.selectedIndex = 0;
    this.value = "";
    this.disabled = false;
    this.checked = false;
    this.open = false;
    this._handlers = {};
  }
  get className() { return this._class; }
  set className(v) {
    this._class = String(v || "");
    this.classList = new ClassSet();
    this._class.split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c));
  }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent).join("");
  }
  set textContent(v) {
    this._text = String(v === undefined || v === null ? "" : v);
    this.children = [];
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) {
    this._html = String(v === undefined || v === null ? "" : v);
    this.children = [];
    if (this.tagName === "SELECT") this._parseOptions(this._html);
  }
  _parseOptions(html) {
    const re = /<option value="([^"]*)"([^>]*)>([\s\S]*?)<\/option>/g;
    const opts = [];
    let m;
    while ((m = re.exec(html)) !== null) {
      const opt = new El("option");
      opt.value = m[1];
      opt.textContent = m[3].replace(/<[^>]+>/g, "");
      opt.selected = /selected/.test(m[2]);
      opts.push(opt);
    }
    this.options = opts;
    const sel = opts.findIndex((o) => o.selected);
    this.selectedIndex = sel >= 0 ? sel : 0;
  }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  prepend(c) { c.parent = this; this.children.unshift(c); return c; }
  insertBefore(c, ref) {
    const i = this.children.indexOf(ref);
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    c.parent = this;
    return c;
  }
  after(node) {
    const p = this.parent;
    if (!p) return;
    const i = p.children.indexOf(this);
    p.children.splice(i + 1, 0, node);
    node.parent = p;
  }
  remove() {
    const p = this.parent;
    if (!p) return;
    const i = p.children.indexOf(this);
    if (i >= 0) p.children.splice(i, 1);
    this.parent = null;
  }
  addEventListener(type, fn) {
    (this._handlers[type] = this._handlers[type] || []).push(fn);
  }
  dispatchEvent(evt) {
    (this._handlers[evt.type] || []).forEach((fn) => fn({ target: this, ...evt }));
    return true;
  }
  closest(sel) {
    let node = this;
    while (node) {
      if (matches(node, sel)) return node;
      node = node.parent;
    }
    return null;
  }
  querySelector(sel) { return findAll(this, sel)[0] || null; }
  querySelectorAll(sel) { return findAll(this, sel); }
}

function matches(el, sel) {
  if (!el || !el.tagName) return false;
  if (sel.startsWith("#")) return el.id === sel.slice(1);
  if (sel.startsWith(".")) return el.classList.contains(sel.slice(1));
  if (sel.startsWith("[") && sel.endsWith("]")) {
    const name = sel.slice(1, -1);
    const key = name.startsWith("data-") ? name.slice(5) : name;
    return Boolean(el.dataset) && key in el.dataset;
  }
  return el.tagName === sel.toUpperCase();
}

function findAll(root, sel) {
  const out = [];
  const walk = (node) => {
    node.children.forEach((c) => {
      if (matches(c, sel)) out.push(c);
      walk(c);
    });
  };
  walk(root);
  return out;
}

// 构建 DOM 骨架（与 index.html 中 id 对应）
const body = new El("body");
const registry = {};
function mk(tag, id, cls) {
  const el = new El(tag);
  if (id) { el.id = id; registry["#" + id] = el; }
  if (cls) el.className = cls;
  body.appendChild(el);
  return el;
}
body.dataset.uiBuild = "v20261109-ui2";
const chatMessages = mk("div", "chat-messages", "chat-messages");
const chatInput = mk("input", "chat-input");
const runDetailBox = mk("input", "chk-run-detail");
runDetailBox.checked = true;
const memoryBtn = mk("button", "btn-memory", "mini-btn secondary");
const historySel = mk("select", "chat-history");
const featureHints = mk("div", "feature-hints", "feature-hints hidden");
const patrolStatusEl = mk("div", "patrol-status", "log");
const queryPlanBox = mk("input", "chk-query-plan");
// RAG 检索面板（index.html §知识库检索）：口径冲突的展示在这里验证
const ragInput = mk("input", "rag-input");
const ragButton = mk("button", "btn-rag", "mini-btn");
const ragResults = mk("div", "rag-results", "citations");
const ragScope = mk("div", "rag-scope", "muted");
const modalOverlay = mk("div", "modal-overlay", "modal-overlay hidden");
const approvalRows = mk("tbody", "approval-rows");
const afterSalesRows = mk("tbody", "after-sales-rows");
const modalTitle = mk("div", "modal-title");
const modalBody = mk("div", "modal-body");
const factsUnderbar = mk("div", "facts-underbar", "facts-underbar");
const factsMeta = mk("span", "facts-underbar-meta", "muted");
const factsBody = mk("pre", "facts-underbar-body", "facts-pre facts-underbar-body");
const wsUnderbar = mk("div", "working-set-underbar", "working-set-underbar");
const wsMeta = mk("span", "working-set-underbar-meta", "muted");
const wsBody = mk("div", "working-set-underbar-body", "working-set-body");
const wsCopy = mk("button", "working-set-copy", "mini-btn secondary");
const cardHit = mk("div", "card-hit", "card clickable");
const latP50 = mk("b", "m-lat-p50");
const latP95 = mk("b", "m-lat-p95");
const mHit = mk("div", "m-hit", "value");
const mChatStreams = mk("b", "m-chat-streams");
const cacheBreakdownBox = mk("div", "cache-breakdown", "muted");
// 顶栏徽标（loadHealth 写入）：存储徽标要同时反映关系库与向量库
const badgeDb = mk("b", "badge-db");
const badgeCache = mk("b", "badge-cache");
const badgeLlm = mk("b", "badge-llm");
const badgeMcp = mk("b", "badge-mcp");

const documentHandlers = {};
const document = {
  body,
  hidden: false,
  querySelector: (sel) => registry[sel] || findAll(body, sel)[0] || null,
  querySelectorAll: (sel) => findAll(body, sel),
  createElement: (tag) => new El(tag),
  getElementById: (id) => registry["#" + id] || null,
  addEventListener: (type, fn) => { (documentHandlers[type] = documentHandlers[type] || []).push(fn); },
};

const storage = {};
const localStorage = {
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => { storage[k] = String(v); },
  removeItem: (k) => { delete storage[k]; },
};

// 浏览器 API 垫片（进度卡用 performance/setInterval；流式用 fetch+ReadableStream+TextDecoder）
const windowHandlers = {};
const sandboxWindow = {
  addEventListener: (type, fn) => { (windowHandlers[type] = windowHandlers[type] || []).push(fn); },
  removeEventListener: () => {},
  location: { href: "http://127.0.0.1:8000/", pathname: "/", replace: () => {} },
};

function chatResponse({ withDetail = true, withPlan = true } = {}) {
  const body = {
    session_id: "s1",
    reply: "可用库存 693 件（res_aaa111bbb2）",
    tool_trace: [],
    metadata_version: 1,
    query_plan: withPlan ? detail().plan : null,
    clarify: null,
    cited_result_ids: ["res_aaa111bbb2"],
    memory: { facts_version: 2, working_version: 3 },
    session_references: [],
    run_detail: withDetail ? detail() : null,
    features: ["run_detail", "query_plan", "chat_stream", "latency"],
  };
  return body;
}

// 让流式端点返回一段真实 SSE 文本
function sseResponse(events) {
  let i = 0;
  const enc = new TextEncoder();
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () => (i >= events.length
          ? { done: true, value: undefined }
          : { done: false, value: enc.encode(events[i++]) }),
      }),
    },
    text: async () => "",
  };
}

function streamEvents(payload) {
  return [
    'event: stage\ndata: {"stage":"plan","label":"解析查询计划","elapsed_ms":12}\n\n',
    'event: stage\ndata: {"stage":"memory","label":"读取会话记忆","elapsed_ms":30}\n\n',
    'event: stage\ndata: {"stage":"agent","label":"调用 DeepSeek（ReAct 工具循环）","elapsed_ms":40}\n\n',
    'event: llm_start\ndata: {"call":1,"label":"模型调用 #1","elapsed_ms":50}\n\n',
    'event: llm_end\ndata: {"call":1,"kind":"tool_call","duration_ms":1300,"input_tokens":900,"output_tokens":40,"elapsed_ms":1350}\n\n',
    'event: tool_start\ndata: {"seq":1,"tool":"get_inventory","label":"取数：get_inventory","elapsed_ms":1360}\n\n',
    'event: tool_end\ndata: {"seq":1,"tool":"get_inventory","status":"ok","elapsed_ms":1380,"result_id":"res_aaa111bbb2","as_of":"2026-11-04","row_count":0,"truncated":false}\n\n',
    'event: llm_start\ndata: {"call":2,"label":"模型调用 #2","elapsed_ms":1390}\n\n',
    'event: delta\ndata: {"text":"可用库存 ","elapsed_ms":1500}\n\n',
    'event: delta\ndata: {"text":"693 件（res_aaa111bbb2）","elapsed_ms":1600}\n\n',
    'event: llm_end\ndata: {"call":2,"kind":"answer","duration_ms":900,"input_tokens":950,"output_tokens":120,"elapsed_ms":2290}\n\n',
    'event: tick\ndata: {"elapsed_ms":2400}\n\n',
    'event: done\ndata: ' + JSON.stringify(payload) + '\n\n',
  ];
}

const canned = {
  "/api/health": { status: "ok", database: "sqlite", cache: "fakeredis", llm_model: "deepseek-flash",
                   mcp_transport: "memory", scheduler_enabled: false, scheduler_next_run: null,
                   scheduler_note: "定时任务已关闭（Demo 默认）",
                   features: ["run_detail", "query_plan", "chat_stream", "latency"],
                   assets: { "app.js": "abc12345", "style.css": "def67890" } },
  "/api/metrics": { sku_count: 300, anomaly_count: 46, scope: { role: "admin" },
                    cache: { hit_rate: 0.62 }, llm: { calls: 3, input_tokens: 100, output_tokens: 20,
                      sources: { chat: { calls: 2, input_tokens: 80, output_tokens: 15 },
                                 patrol: { calls: 1, input_tokens: 20, output_tokens: 5 } },
                      departments: { "管理员": { calls: 3, input_tokens: 100, output_tokens: 20 } } },
                    queue: { pending: 0, length: 0, chat_streams: 1, departments: {},
                             note: "巡检走 Redis Stream 队列；对话是同步 ReAct（SSE 直推），不计入队列" },
                    departments: {},
                    latency: { overall: { count: 12, p50: 18, p95: 240, p99: 480, max: 480, avg: 60, min: 3 },
                               categories: { api: { count: 6, p50: 18, p95: 240, p99: 480, max: 480, avg: 60, min: 3 } },
                               labels: { api: "接口耗时（Agent HTTP）" },
                               window: 1000, note: "进程内最近 1000 次采样，重启清零" } },
  "/api/chat/sessions": { count: 3, retention_hours: 24, pruned: 0, items: [    { session_id: "s1", title: "ST-01-C1-M 库存", message_count: 4, created_at: "2026-11-09T10:00:00", updated_at: "2026-11-09T10:05:00" },
    { session_id: "s2", title: "断货风险诊断", message_count: 2, created_at: "2026-11-09T11:00:00", updated_at: "2026-11-09T11:06:00" },
    { session_id: "s3", title: "空会话（旧版清空）", message_count: 0, created_at: "2026-11-09T09:00:00", updated_at: "2026-11-09T09:01:00" }] },
  "/api/actions/list": { window_hours: 4, count: 1, items: [
    { approval_id: "appr-1", sku_id: "ST-01-C1-M", qty: 500, amount: 50000, reason: "低库存",
      status: "pending_confirmation", po_no: "PO-1", approver: null, decided_at: null,
      erp_status: "draft", current_available: 42, current_on_hand: 60, daily_avg_7d: 6.0,
      warehouse_id: "WH-HZ", created_at: "2026-11-09T12:00:00" }] },
  "/api/actions/after-sales": { status: "all", count: 2, pending: 1, note: "售后工单只建草稿", items: [
    { ticket_id: "tkt-1", order_id: "MO-1", sku_id: "ST-01-C1-M", reason: "尺码不合适", qty: 1,
      status: "pending_confirmation", approver: null, decided_at: "",
      created_at: "2026-11-09T12:00:00", note: "" },
    { ticket_id: "tkt-2", order_id: "MO-2", sku_id: "ST-01-C1-M", reason: "质量问题", qty: 2,
      status: "approved", approver: "admin", decided_at: "2026-11-09T12:30:00",
      created_at: "2026-11-09T11:00:00", note: "客户举证充分" }] },
  "/api/memory/sessions/s1": { session_id: "s1", facts_card: "<analysis-facts v2>ok</analysis-facts>",
    facts_version: 2, facts_trigger: "messages>12", facts_as_of: "2026-11-09T12:00:00",
    working_set: { skus: { "ST-01-C1-M": { hits: 3 } }, metrics: { 可用库存: {} }, version: 3 },
    working_set_text: "<working-set version=3>\nSKU: ST-01-C1-M\n关注指标: 可用库存\n</working-set>",
    working_version: 3, updated_at: "2026-11-09T12:00:00",
    latest_query_plan: { plan_id: "qp_test", intent: "lookup" },
    recent_tool_results: [{ result_id: "res_aaa111bbb2", tool: "get_inventory", as_of: "2026-11-04", truncated: false }],
    turn_count: 4 },
  "/api/chat/s1": { session_id: "s1", messages: [
    { role: "user", content: "ST-01-C1-M 库存", tool_trace: [] },
    { role: "assistant", content: "可用库存 693 件（res_aaa111bbb2）", tool_trace: [], run_detail: detail() }] },
};

function detail() {
  return {
    turn_id: "t_smoke0001", as_of: "2026-11-09T12:00:00", elapsed_ms: 12480,
    llm: { model: "deepseek-flash", calls: 2, input_tokens: 4210, output_tokens: 860, errors: 0,
           calls_detail: [{ index: 1, start_offset_ms: 120, elapsed_ms: 9120, kind: "tool_call",
                            input_tokens: 2100, output_tokens: 300 },
                          { index: 2, start_offset_ms: 9500, elapsed_ms: 2900, kind: "answer",
                            input_tokens: 2110, output_tokens: 560 }] },
    timeline: [
      { kind: "stage", offset_ms: 0, label: "读取会话记忆（事实卡 + 工作集）", stage: "memory", elapsed_ms: null },
      { kind: "stage", offset_ms: 120, label: "调用 DeepSeek（ReAct 工具循环）", stage: "agent", elapsed_ms: null },
      { kind: "llm", offset_ms: 120, label: "模型调用 #1", kind_label: "决定取数", elapsed_ms: 9120,
        input_tokens: 2100, output_tokens: 300 },
      { kind: "tool", offset_ms: 9300, label: "get_inventory", elapsed_ms: 420,
        result_id: "res_aaa111bbb2", as_of: "2026-11-04", agent_cached: false,
        truncated: false, row_count: 0, status: "ok" },
      { kind: "llm", offset_ms: 9500, label: "模型调用 #2", kind_label: "生成答复", elapsed_ms: 2900,
        input_tokens: 2110, output_tokens: 560 },
      { kind: "stage", offset_ms: 12400, label: "整理答复（本地汇总证据链，无模型调用）", stage: "answer", elapsed_ms: null },
    ],
    phases: { llm_ms: 12020, tools_ms: 420, other_ms: 40, total_ms: 12480, llm_calls: 2, tool_calls: 1 },
    plan: { plan_id: "qp_smoke0001", intent: "lookup", confidence: 0.8,
            entities: { skus: ["ST-01-C1-M"], styles: ["ST-01"], sizes: [], colors: [] },
            time_range: { label: "" }, required_data: ["inventory"], retrieval_keywords: ["ST-01-C1-M"],
            assumptions: [], metric_version: "2026Q1", resolved_from: "", blocked: false },
    steps: [{ seq: 1, tool: "get_inventory", args: '{"sku_id": "ST-01-C1-M"}', status: "ok",
              elapsed_ms: 420, start_offset_ms: 9300, result_id: "res_aaa111bbb2", as_of: "2026-11-04",
              row_count: 0, truncated: false, artifact_path: "", summary: "available=693", error: "" }],
    memory: {
      context: { facts_card: "<analysis-facts v2>\n- 可用库存 693（res_aaa111bbb2）\n</analysis-facts>",
                 working_set: "<working-set v3>\nSKU: ST-01-C1-M\n</working-set>",
                 facts_version: 2, working_version: 3, trigger: "messages>12", chars: 120 },
      previous: { facts_version: 1, working_version: 2, facts_card: "", trigger: "rules" },
      facts_version: 2, working_version: 3, facts_delta: 1,
    },
    references: { result_ids: ["res_aaa111bbb2"], artifacts: [], analyses: [] },
    permission: { department: "管理员", allowed: null, denied_steps: [] },
    agent: { id: 'diagnostician', version: 'abc12345', name: '库存诊断专家', routed_by: 'keyword:断码',
             fallback_from: '', retries: 0, model: 'deepseek-flash',
             model_requested: 'deepseek-flash', model_used: 'deepseek-flash', fallback_used: false,
             tools: ['get_inventory', 'get_sell_through'], tool_count: 16, tools_all: false,
             memory_scope: 'session', max_reflection_rounds: 0 },
    grounding: { status: "passed", rewritten: false, issues: [], after: [],
                 citations: { cited: ["res_aaa111bbb2"], unknown: [], count: 1 },
                 numbers: { claims: ["693件"], count: 1, unverified: [], unverified_count: 0 },
                 elapsed_ms: 3 },
  };
}

let lastFetch = null;

const sandbox = {
  document, localStorage, console,
  location: sandboxWindow.location,
  setTimeout, clearTimeout,
  setInterval: (fn, ms) => setInterval(fn, ms),
  clearInterval: (id) => clearInterval(id),
  performance: { now: () => Date.now() },
  URL, TextEncoder, TextDecoder, ReadableStream: function ReadableStream() {},
  fetch: async (url, opts) => {
    lastFetch = { url: String(url), opts: opts || {} };
    if (String(url).includes("/api/chat/stream")) return sseResponse(streamEvents(chatResponse()));
    return { ok: true, status: 200, json: async () => ({}), text: async () => "" };
  },
  navigator: { clipboard: { writeText: async () => {} } },
};
sandbox.window = sandboxWindow;
Object.assign(sandboxWindow, {
  document, localStorage, console, fetch: sandbox.fetch,
  location: sandboxWindow.location, __UI_BUILD: undefined,
});
sandbox.globalThis = sandbox;
sandbox.addEventListener = sandboxWindow.addEventListener;

// 去掉 init IIFE（它依赖完整页面），其余函数保留；并把 const 声明的状态对象挂到全局便于断言
const initIdx = APP_JS.indexOf("(async function init() {");
const stripped = (initIdx > 0 ? APP_JS.slice(0, initIdx) : APP_JS) +
  "\n;globalThis.__ui = { state, chatState };\n";
const ctx = vm.createContext(sandbox);
vm.runInContext(stripped, ctx, { filename: "app.js" });
const ui = sandbox.__ui;

// 用 stub 替换网络层（记录调用，便于断言"新会话不再清空旧会话"）
const calls = [];
ctx.api = async (p, opts) => {
  const clean = String(p).split("?")[0];
  calls.push({ path: clean, method: (opts && opts.method) || "GET" });
  if (clean in canned) return JSON.parse(JSON.stringify(canned[clean]));
  if (clean.startsWith("/api/chat/s")) return JSON.parse(JSON.stringify(canned["/api/chat/s1"]));
  if (clean.startsWith("/api/chat")) return chatResponse();
  return {};
};

// 模拟浏览器事件（走真实的事件委托路径）
function fireDocument(type, target, extra) {
  (documentHandlers[type] || []).forEach((fn) =>
    fn(Object.assign({ type, target, preventDefault() {}, stopPropagation() {} }, extra || {})));
}

(async () => {
  console.log("== 1. 函数与绑定 ==");
  ctx.bindGlobalHandlers();   // 真实入口：事件委托（init 里第一步）
  check("attachRunDetail 已定义", typeof ctx.attachRunDetail === "function");
  check("openMemoryPanel 已定义", typeof ctx.openMemoryPanel === "function");
  check("loadChatSession 已定义", typeof ctx.loadChatSession === "function");
  check("运行详情开关存在", !!document.querySelector("#chk-run-detail"));

  ctx.initRunDetailToggle();
  check("默认勾选（首次访问）", document.querySelector("#chk-run-detail").checked === true);

  console.log("== 2. 发送对话 → 渲染运行详情 ==");
  ui.state.token = "smoke";
  ui.state.user = { username: "admin", role: "admin", department: null };
  ui.chatState.sessionId = null;
  await ctx.sendChat("ST-01-C1-M 当前可用库存？");
  const wraps = findAll(body, ".run-detail-wrap");
  const lines = findAll(body, ".evidence-line");
  check("生成了运行详情区块", wraps.length === 1, wraps.length);
  check("生成了证据链", lines.length === 1, lines.length);
  check("证据链含 result_id", lines.length && lines[0].textContent.includes("res_aaa111bbb2"),
        lines.length ? lines[0].textContent : null);
  check("证据链含事实卡版本", lines.length && lines[0].textContent.includes("事实卡 v2"));
  const detailsEl = wraps[0] ? wraps[0].querySelector(".run-detail") : null;
  check("本轮详情默认展开", !!detailsEl && detailsEl.open === true);
  check("详情分 7 段（新增时间轴 + 溯源自检）", detailsEl ? detailsEl.querySelectorAll(".run-section").length === 7
        : false, detailsEl ? detailsEl.querySelectorAll(".run-section").length : null);
  check("溯源自检段展示结论",
        !!detailsEl && String(detailsEl.textContent).includes("溯源自检")
        && String(detailsEl.textContent).includes("pass") === false);
  check("自检通过时不弹告警条", findAll(body, ".grounding-note").length === 0,
        findAll(body, ".grounding-note").length);
  check("证据链标出 agent@version", lines[0].textContent.includes("agent:diagnostician@abc12345"),
        lines[0].textContent.slice(0, 80));
  const kvCells = findAll(body, ".kv-cell").map((el) => String(el.innerHTML || ""));
  check("运行详情展示 Agent 行", kvCells.some((h) => h.includes("Agent") && h.includes("diagnostician")),
        kvCells.length);
  check("工具数按 tool_count 展示", kvCells.some((h) => h.includes("工具 16 个")), kvCells.length);
  const timelineRows = detailsEl ? detailsEl.querySelectorAll(".row-llm") : [];
  check("时间轴里有模型调用行（秒级归因）", timelineRows.length === 2, timelineRows.length);
  const timelineSummary = detailsEl ? detailsEl.querySelector(".timeline-summary") : null;
  check("时间轴给出耗时构成（模型/工具/其它）",
        !!timelineSummary && String(timelineSummary.textContent).includes("模型 12.0s")
        && String(timelineSummary.textContent).includes("工具 420ms"),
        timelineSummary ? timelineSummary.textContent : null);
  check("工具明细仍在（时间轴之外单独一段）",
        !!detailsEl && String(detailsEl.textContent).includes("工具调用明细"));
  const factsBox = wraps[0] ? wraps[0].querySelector(".facts-block") : null;
  const facts = factsBox ? factsBox.querySelector("pre") : null;
  check("事实卡内容可见", !!facts && String(facts.textContent).includes("analysis-facts"),
        facts ? facts.textContent : null);
  check("详情区块未隐藏", wraps[0] && !wraps[0].classList.contains("hidden"));

  console.log("== 2a. 真·流式：delta 先写入气泡，done 时用最终答复覆盖（不重复出气泡）==");
  const assistantBubbles = findAll(chatMessages, ".chat-msg").filter((el) => el.classList.contains("assistant"));
  check("只有一条助手气泡（流式不重复追加）", assistantBubbles.length === 1, assistantBubbles.length);
  check("流式结束后不再是 streaming 态",
        assistantBubbles.every((el) => !el.classList.contains("streaming")));
  check("气泡内容 = done 里的最终答复",
        String(assistantBubbles[0].textContent).includes("可用库存 693 件（res_aaa111bbb2）"),
        String(assistantBubbles[0].textContent));
  const progressLeft = findAll(chatMessages, ".progress-card");
  check("流式开始后进度卡已被移除", progressLeft.length === 0, progressLeft.length);

  console.log("== 2b. 两个开关的产物各自带标签（蓝=Query 重写 / 绿=运行详情）==");
  const planCards = findAll(body, ".query-plan-card");
  check("生成了 Query 重写卡片", planCards.length === 1, planCards.length);
  const planBadge = planCards[0] ? planCards[0].querySelector(".badge-plan") : null;
  check("计划卡片带蓝色『Query 重写』徽标", !!planBadge && String(planBadge.textContent).includes("Query 重写"),
        planBadge ? planBadge.textContent : null);
  check("计划卡片有归属说明", planCards[0] &&
        !!planCards[0].querySelector(".artifact-foot-plan"));
  const runBadgeOnLine = lines[0] ? lines[0].querySelector(".badge-run") : null;
  check("证据链带绿色『显示运行详情』徽标",
        !!runBadgeOnLine && String(runBadgeOnLine.textContent).includes("显示运行详情"),
        runBadgeOnLine ? runBadgeOnLine.textContent : null);
  const runBadgeOnPanel = detailsEl ? detailsEl.querySelector(".badge-run") : null;
  check("运行详情面板也带绿标", !!runBadgeOnPanel);
  check("运行详情有归属说明", !!detailsEl && !!detailsEl.querySelector(".artifact-foot-run"));
  const sameSourceNote = detailsEl ? findAll(detailsEl, "div")
    .find((el) => String(el.textContent || "").includes("同一份计划")) : null;
  check("面板标注了与蓝卡同源", !!sameSourceNote,
        detailsEl ? findAll(detailsEl, "div").map((e) => String(e.textContent || "").slice(0, 40))
          .filter((t) => t.includes("同源") || t.includes("同一份")).slice(0, 2) : null);

  console.log("== 2c. 发送框下方常驻当前事实卡 ==");
  const factsBar = document.querySelector("#facts-underbar-body");
  const factsMeta = document.querySelector("#facts-underbar-meta");
  check("事实卡常驻区存在", !!factsBar);
  check("常驻区显示本轮事实卡内容",
        !!factsBar && String(factsBar.textContent).includes("analysis-facts"),
        factsBar ? String(factsBar.textContent).slice(0, 60) : null);
  check("常驻区显示版本信息",
        !!factsMeta && String(factsMeta.textContent).includes("事实卡 v2"),
        factsMeta ? String(factsMeta.textContent) : null);

  console.log("== 2d. 事实卡下方的实体表（工作集）常驻区 ==");
  const wsMeta = document.querySelector("#working-set-underbar-meta");
  const wsBody = document.querySelector("#working-set-underbar-body");
  check("实体表常驻区存在", !!wsBody && !!wsMeta);
  const wsTable = wsBody ? wsBody.querySelector(".working-set-table") : null;
  check("实体表渲染成表格", !!wsTable, wsTable ? "有表格" : (wsBody ? String(wsBody.textContent).slice(0, 40) : null));
  const wsRows = wsTable ? wsTable.querySelectorAll("tr") : [];
  check("表格里有 SKU 行", wsRows.some((r) => String(r.textContent).includes("ST-01-C1-M")),
        wsRows.slice(0, 3).map((r) => String(r.textContent)));
  check("实体表显示版本与更新信息",
        String(wsMeta.textContent).includes("工作集 v3"),
        String(wsMeta.textContent));
  const wsCopy = document.querySelector("#working-set-copy");
  check("实体表有复制按钮且带可复制文本", !!wsCopy && Boolean(wsCopy.dataset && wsCopy.dataset.text));

  console.log("== 2e. 旧记录（没有时间轴字段）不显示假数字 ==");
  const oldDetail = detail();
  delete oldDetail.timeline;
  delete oldDetail.phases;
  delete oldDetail.llm.calls_detail;
  const oldSection = ctx.timelineSection(oldDetail);
  const oldText = String(oldSection.textContent);
  check("旧记录给出「没有采集」说明", oldText.includes("没有时间轴"), oldText.slice(0, 60));
  check("旧记录不伪造耗时构成", !oldText.includes("其它") && !oldText.includes("模型 "), oldText.slice(0, 60));

  console.log("== 3. 取消勾选 → 详情隐藏（证据链常显）==");
  const box = document.querySelector("#chk-run-detail");
  box.checked = false;
  box.dispatchEvent({ type: "change" });
  check("取消勾选后详情隐藏", wraps[0].classList.contains("hidden"));
  check("证据链仍然可见", lines[0] && !lines[0].classList.contains("hidden"));
  box.checked = true;
  box.dispatchEvent({ type: "change" });
  check("重新勾选后恢复显示", !wraps[0].classList.contains("hidden"));

  console.log("== 4. 记忆面板（点击按钮 → 事件委托）==");
  fireDocument("click", memoryBtn);
  await new Promise((r) => setTimeout(r, 30));
  check("点击按钮后弹窗已打开", !modalOverlay.classList.contains("hidden"));
  check("标题正确", String(modalTitle.textContent).includes("记忆面板"), modalTitle.textContent);
  check("弹窗含事实卡", String(modalBody.innerHTML).includes("analysis-facts"));
  check("弹窗含取数台账", String(modalBody.innerHTML).includes("res_aaa111bbb2"));
  check("按钮读取结束后恢复可点", memoryBtn.disabled === false, memoryBtn.disabled);

  console.log("== 5. 历史会话下拉与回放 ==");
  await ctx.loadChatSessions();
  check("下拉已填充历史会话", historySel.options.length === 3, historySel.options.length);
  check("占位提示明确", historySel.options[0] && historySel.options[0].textContent.includes("选择会话回放"),
        historySel.options[0] ? historySel.options[0].textContent : null);
  // 聚焦下拉也应刷新列表
  fireDocument("focusin", historySel);
  await new Promise((r) => setTimeout(r, 20));
  check("聚焦下拉会刷新列表", historySel.options.length === 3, historySel.options.length);

  historySel.value = "s1";
  fireDocument("change", historySel);
  await new Promise((r) => setTimeout(r, 30));
  const msgs = findAll(chatMessages, ".chat-msg");
  check("回放了历史消息", msgs.length >= 3, msgs.length);
  check("回放后 sessionId 指向历史会话", ui.chatState.sessionId === "s1");
  check("回放也带回运行详情", findAll(chatMessages, ".run-detail-wrap").length === 1,
        findAll(chatMessages, ".run-detail-wrap").length);
  const replayNote = msgs.find((m) => m.classList.contains("system"));
  check("回放有说明提示", !!replayNote && String(replayNote.textContent).includes("已回放历史会话"));

  console.log("== 6. 新会话：不清空旧会话，且刷新历史列表 ==");
  const before = calls.filter((c) => c.path.endsWith("/reset")).length;
  await ctx.resetChat();
  check("新会话后 sessionId 置空", ui.chatState.sessionId === null);
  check("新会话不再调用后端 reset（旧会话可回放）",
        calls.filter((c) => c.path.endsWith("/reset")).length === before,
        calls.filter((c) => c.path.endsWith("/reset")).length);
  check("新会话后下拉仍有历史", historySel.options.length === 3, historySel.options.length);

  console.log("== 7. 无当前会话时点记忆面板 → 会话选择器 ==");
  fireDocument("click", memoryBtn);
  await new Promise((r) => setTimeout(r, 40));
  const picker = findAll(modalBody, "button").find((el) => String(el.textContent).includes("查看记忆"));
  check("弹出会话选择器（带可点会话）", !!picker && findAll(modalBody, "tr").length >= 2,
        findAll(modalBody, "button").length);
  if (picker) {
    await picker.onclick();
    await new Promise((r) => setTimeout(r, 40));
    check("选会话后重新打开记忆面板", String(modalTitle.textContent).includes("记忆面板"));
    check("记忆面板带出该会话事实卡", String(modalBody.innerHTML).includes("analysis-facts")
          || findAll(modalBody, "pre").some((p) => String(p.textContent).includes("analysis-facts")));
  }

  console.log("== 8. 实时进度卡（等待毫秒 + 模型调用 + 当前步骤）==");
  const progress = ctx.createProgressCard();
  chatMessages.appendChild(progress.el);
  ctx.handleStreamEvent("stage", { stage: "plan", label: "解析查询计划", elapsed_ms: 12 }, progress);
  ctx.handleStreamEvent("stage", { stage: "plan_done", intent: "lookup", elapsed_ms: 20 }, progress);
  ctx.handleStreamEvent("llm_start", { call: 1, label: "模型调用 #1", elapsed_ms: 25 }, progress);
  ctx.handleStreamEvent("llm_end", { call: 1, kind: "tool_call", duration_ms: 1300,
                                     input_tokens: 900, output_tokens: 40, elapsed_ms: 1330 }, progress);
  ctx.handleStreamEvent("tool_start", { seq: 1, tool: "get_inventory", label: "取数：get_inventory", elapsed_ms: 1340 }, progress);
  ctx.handleStreamEvent("tool_end", { seq: 1, tool: "get_inventory", status: "ok", elapsed_ms: 120,
                                      result_id: "res_aaa111bbb2", as_of: "2026-11-04", truncated: false }, progress);
  ctx.handleStreamEvent("llm_start", { call: 2, label: "模型调用 #2", elapsed_ms: 1500 }, progress);
  ctx.handleStreamEvent("llm_end", { call: 2, kind: "answer", duration_ms: 900,
                                     input_tokens: 950, output_tokens: 120, elapsed_ms: 2400 }, progress);
  ctx.handleStreamEvent("tick", { elapsed_ms: 2500 }, progress);
  await new Promise((r) => setTimeout(r, 120));
  const progressTime = progress.el.querySelector(".progress-time");
  check("进度卡显示已等待时间", !!progressTime && /已等待 \d+\.\d+s/.test(String(progressTime.textContent)),
        progressTime ? progressTime.textContent : null);
  const stageChips = progress.el.querySelector(".progress-stages").children.filter((c) => c.tagName === "SPAN");
  check("阶段条渲染（计划/记忆/模型/取数/答复）", stageChips.length === 5, stageChips.length);
  check("计划阶段已点亮为完成", stageChips[0] && String(stageChips[0].className).includes("step-ok"),
        stageChips[0] ? stageChips[0].className : null);
  const stepRows = progress.el.querySelector(".progress-steps").children;
  check("步骤区共 3 行（模型 #1 / 工具 / 模型 #2）", stepRows.length === 3, stepRows.length);
  const llmRows = findAll(progress.el, ".progress-llm");
  check("进度卡列出模型调用行", llmRows.length === 2, llmRows.length);
  check("模型行写明用途与 token",
        llmRows.length === 2
        && String(llmRows[0].textContent).includes("决定取数")
        && String(llmRows[1].textContent).includes("生成答复")
        && String(llmRows[0].textContent).includes("in 900"),
        llmRows.map((r) => String(r.textContent)));
  check("模型返回后阶段文字切到答复汇总",
        String(progress.el.querySelector(".progress-stage").textContent).includes("答复已生成"),
        String(progress.el.querySelector(".progress-stage").textContent));
  const toolRow = stepRows.filter((r) => String(r.textContent).includes("get_inventory"));
  check("工具行仍显示 result_id 与耗时",
        toolRow.length === 1 && String(toolRow[0].textContent).includes("res_aaa111bbb2")
        && String(toolRow[0].textContent).includes("120ms"),
        toolRow.length ? String(toolRow[0].textContent) : null);
  progress.remove();
  check("进度卡可移除", findAll(chatMessages, ".progress-card").length === 0);

  console.log("== 9. 失败可见化：响应缺 run_detail 时给出黄条并兜底刷新事实卡 ==");
  const realApi = ctx.api;
  const realFetch = sandbox.fetch;
  const mark = calls.length;
  // 流式与 POST 都返回"缺 run_detail"的响应，模拟旧后端
  sandbox.fetch = async () => sseResponse(streamEvents(chatResponse({ withDetail: false, withPlan: false })));
  ctx.api = async (p, opts) => {
    const clean = String(p).split("?")[0];
    calls.push({ path: clean, method: (opts && opts.method) || "GET" });
    if (clean === "/api/chat") return chatResponse({ withDetail: false, withPlan: false });
    return realApi(p, opts);
  };
  ui.chatState.sessionId = "s1";
  await ctx.sendChat("ST-01-C1-M 库存（缺 run_detail 场景）");
  const warns = findAll(chatMessages, ".chat-warn");
  check("缺 run_detail 时出现黄色告警", warns.length >= 1,
        warns.map((w) => String(w.textContent).slice(0, 40)));
  const afterMark = calls.slice(mark).map((c) => c.path);
  check("事实卡兜底调用了记忆接口", afterMark.includes("/api/memory/sessions/s1"), afterMark);
  check("兜底后事实卡常驻区有内容",
        String(factsBody.textContent).includes("analysis-facts"),
        String(factsBody.textContent).slice(0, 40));
  ctx.api = realApi;
  sandbox.fetch = realFetch;

  console.log("== 10. 自检面板 ==");
  await ctx.openDiagnostics();
  check("自检弹窗打开", String(modalTitle.textContent).includes("自检"), modalTitle.textContent);
  check("自检含页面/脚本版本检查", String(modalBody.innerHTML).includes("页面 build"));
  check("自检含关键函数状态", String(modalBody.innerHTML).includes("attachRunDetail"));
  check("自检含最近一次对话响应", String(modalBody.innerHTML).includes("run_detail")
        || String(modalBody.innerHTML).includes("hasRunDetail"));

  console.log("== 11. 延迟分位卡片 + 缓存明细 ==");
  ctx.renderLatencyCard(canned["/api/metrics"].latency);
  check("卡片显示 P50", String(latP50.textContent) === "18ms", latP50.textContent);
  check("卡片显示 P95", String(latP95.textContent) === "240ms", latP95.textContent);

  ctx.renderCacheCard({ hits: 0, misses: 0, hit_rate: 0 });
  const hitEl = document.querySelector("#m-hit");
  check("无采样时显示『暂无采样』而非 0%", String(hitEl.textContent) === "暂无采样", hitEl.textContent);
  ctx.renderCacheCard({ hits: 3, misses: 1, hit_rate: 0.75 });
  check("有采样时显示百分比", String(hitEl.textContent) === "75%", hitEl.textContent);

  ui.state.metrics = canned["/api/metrics"];
  const realApi2 = ctx.api;
  ctx.api = async (p, opts) => {
    const clean = String(p).split("?")[0];
    calls.push({ path: clean, method: (opts && opts.method) || "GET" });
    if (clean === "/api/cache/breakdown") {
      return { agent: { hits: 3, misses: 1, writes: 2, hit_rate: 0.75 },
               platform: { hits: 9, misses: 3, writes: 9, hit_rate: 0.75 },
               platform_error: null,
               ttl: { platform: 120, mcp: 600, rag: 600 },
               note: "命中率 = 命中 ÷(命中+未命中)" };
    }
    return realApi2(p, opts);
  };
  ctx.openLatencyBreakdown();
  await ctx.loadCacheBreakdown();
  await new Promise((r) => setTimeout(r, 30));
  check("明细弹窗打开", String(modalTitle.textContent).includes("缓存"), modalTitle.textContent);
  const cacheHtml = String(cacheBreakdownBox.innerHTML || "");
  check("缓存明细含 Agent 层", cacheHtml.includes("Agent 旁路"), cacheHtml.slice(0, 60));
  check("缓存明细含中台层", cacheHtml.includes("数据中台"));
  check("缓存明细含 TTL 说明", cacheHtml.includes("TTL"));
  check("延迟明细含分类", String(modalBody.innerHTML).includes("接口耗时"));
  ctx.api = realApi2;

  console.log("== 12. 版本比对：页面与脚本不一致时给出横幅 ==");
  body.dataset.uiBuild = "vOLD-build";
  ctx.checkUiBuild();
  const banner = findAll(body, ".stale-banner")[0];
  check("版本不一致时出现黄色横幅", !!banner, banner ? String(banner.textContent).slice(0, 40) : null);
  if (banner) banner.remove();
  body.dataset.uiBuild = "v20261110-ui6";

  console.log("== 13. 三张卡片的数据来源（LLM 来源 / 队列对话进行中）==");
  ui.state.metrics = JSON.parse(JSON.stringify(canned["/api/metrics"]));
  ctx.renderCacheCard({ hits: 3, misses: 1, hit_rate: 0.75 });
  const llmEl = document.querySelector("#m-llm");
  ctx.openBreakdown("llm");
  const llmHtml = String(modalBody.innerHTML || "");
  check("LLM 明细含来源分布", llmHtml.includes("来源分布"), llmHtml.slice(0, 60));
  check("LLM 明细含对话来源", llmHtml.includes("对话（ReAct 工具循环）"));
  check("LLM 明细含按部门", llmHtml.includes("按部门"));
  ctx.openBreakdown("queue");
  const queueHtml = String(modalBody.innerHTML || "");
  check("队列明细含对话通道", queueHtml.includes("对话通道（SSE 进行中）"), queueHtml.slice(0, 60));
  check("队列明细含口径说明", queueHtml.includes("不计入队列"));
  ctx.renderQueueCard(ui.state.metrics.queue);
  check("队列卡片显示对话进行中数值", String(mChatStreams.textContent) === "1", mChatStreams.textContent);

  console.log("== 14. 功能介绍按勾选动态显示 ==");
  const hints = () => featureHints.children.map((c) => String(c.textContent || ""));
  const setBoxes = (q, r) => {
    queryPlanBox.checked = q;
    runDetailBox.checked = r;
    ctx.renderFeatureHints();
  };
  setBoxes(false, false);
  check("都不勾 → 不显示介绍", hints().length === 0 && featureHints.classList.contains("hidden"),
        { lines: hints(), hidden: featureHints.classList.contains("hidden") });
  setBoxes(true, false);
  check("只勾 Query → 一行计划介绍",
        hints().length === 1 && hints()[0].includes("结构化查询计划"), hints());
  setBoxes(false, true);
  check("只勾运行详情 → 一行详情介绍",
        hints().length === 1 && hints()[0].includes("证据链"), hints());
  setBoxes(true, true);
  check("都勾 → 两行（每行一个功能）",
        hints().length === 2 && hints()[0].includes("结构化查询计划") && hints()[1].includes("证据链"),
        hints());
  setBoxes(false, true);
  featureHints.classList.remove("hidden");

  console.log("== 15. 批量巡检单框 + 登录态文案 ==");
  ctx.setPatrolStatus("巡检中… run=x 已等待 2s");
  check("状态框可写入", String(patrolStatusEl.textContent).includes("巡检中"), patrolStatusEl.textContent);
  ctx.renderPatrolProgress({});
  check("无进度时回到空闲文案", String(patrolStatusEl.textContent).includes("暂无正在进行"), patrolStatusEl.textContent);
  check("不再存在第二个输出框", !document.querySelector("#patrol-log"));

  console.log("== 16. SSE 请求带鉴权头 + 历史会话 24h 提示 ==");
  check("流式请求带 Authorization", Boolean(lastFetch && lastFetch.opts && lastFetch.opts.headers
        && lastFetch.opts.headers.Authorization), lastFetch && lastFetch.opts ? Object.keys(lastFetch.opts.headers || {}) : null);
  await ctx.loadChatSessions();
  check("历史会话下拉标注 24 小时保留", String(historySel.title || "").includes("24"),
        historySel.title);

  console.log("== 17. 人工确认面板：补货 + 售后同卡 ==");
  ui.state.token = "smoke";
  await ctx.loadPendingApprovals();
  const apprRows = approvalRows.children || [];
  const ticketRows = afterSalesRows.children || [];
  const apprHtml = apprRows.map((r) => String(r.innerHTML || "")).join("");
  const ticketHtml = ticketRows.map((r) => String(r.innerHTML || "")).join("");
  check("补货单行渲染", apprRows.length === 1 && apprHtml.includes("ST-01-C1-M"), apprRows.length);
  check("售后工单行渲染（2 条：待确认 + 已确认）", ticketRows.length === 2, ticketRows.length);
  const approveBtns = (ticketHtml.match(/data-action="approve"/g) || []).length;
  const rejectBtns = (ticketHtml.match(/data-action="reject"/g) || []).length;
  check("待确认工单才有确认/驳回按钮", approveBtns === 1 && rejectBtns === 1, { approveBtns, rejectBtns });
  check("已处理工单显示审批账号", ticketHtml.includes("admin"));
  check("售后列表走独立接口", calls.some((c) => c.path === "/api/actions/after-sales"),
        calls.map((c) => c.path));

  console.log("== 18. 流式错误用状态码：409 不降级 POST ==");  const realFetch18 = sandbox.fetch;
  const mark18 = calls.length;
  // 后端在建流前返回 409（同会话并发）：前端必须提示原因，且**不能**回退成 POST /api/chat
  sandbox.fetch = async () => ({
    ok: false,
    status: 409,
    json: async () => ({ detail: "该会话正在处理上一轮，请稍候再发（同一会话不支持并发对话）" }),
    text: async () => "",
  });
  const stages18 = [];
  let conflictErr = null;
  try {
    await ctx.requestChat("并发第二轮", { setStage: (stage, text) => stages18.push(String(text || stage)) });
  } catch (err) {
    conflictErr = err;
  }
  const after18 = calls.slice(mark18).map((c) => c.path);
  check("409 不回退到 POST /api/chat", !after18.includes("/api/chat"), after18);
  check("409 抛出可读错误并标记 noFallback",
        Boolean(conflictErr && conflictErr.noFallback === true
                && String(conflictErr.message).includes("HTTP 409")),
        conflictErr && conflictErr.message);
  check("409 的原因透传到进度提示",
        stages18.some((t) => t.includes("正在处理上一轮")), stages18);
  sandbox.fetch = realFetch18;

  console.log("== 19. 知识库检索：口径冲突展示 ==");
  // 后端 /api/rag/search 在有冲突时返回 conflicts + citations[].is_primary；
  // 面板必须把「采用哪条、为什么、哪条没采用」直接显示出来，否则用户只看到一个数字。
  canned["/api/rag/search"] = {
    scope: "女装部门内部 SOP + 通用 SOP",
    cached: false,
    no_answer: false,
    conflict_check: "ok",
    conflicts: [{
      key: "clearance_trigger_days",
      winner: { display: "45 天", doc_id: "sop-clearance-women", title: "女装部门清仓 SOP",
                authority: "dept", authority_label: "部门细则", version: "v0.9" },
      losers: [{ display: "60 天", doc_id: "sop-clearance", title: "清仓 SOP",
                 authority: "general", version: "v1.2" }],
      reason: "部门细则优先于通用规则（中台治理口径 > 部门细则 > 通用规则 > 历史案例）",
    }],
    hits: [
      { chunk_id: "sop-clearance-women#a1b2c3d4", doc_id: "sop-clearance-women",
        title: "女装部门清仓 SOP", section: "触发条件", text: "可售天数 ≥ 45 天进入换季观察",
        score: 0.51, meta: { department: "女装", authority: "dept" } },
      { chunk_id: "sop-clearance#beef0001", doc_id: "sop-clearance",
        title: "清仓 SOP", section: "触发条件", text: "可售天数 ≥ 60 天触发清仓",
        score: 0.43, meta: { department: "通用", authority: "general" } },
    ],
  };
  ragInput.value = "女装清仓触发天数";
  await ctx.ragSearch();
  const ragHtml = String(ragResults.innerHTML);
  check("RAG 面板标出口径冲突数量", ragHtml.includes("口径冲突 1 处"), ragHtml.slice(0, 80));
  check("冲突块给出胜出口径与依据",
        ragHtml.includes("45 天") && ragHtml.includes("部门细则优先于通用规则"));
  check("冲突块列出未采用的口径", ragHtml.includes("不采用") && ragHtml.includes("60 天"));
  check("命中带权威度标签", ragHtml.includes("部门细则") && ragHtml.includes("通用规则"));
  check("落败文档的命中标为次要口径", ragHtml.includes("次要口径"));
  check("检索范围提示已更新", String(ragScope.textContent).includes("女装"));

  // 语料没声明 key_facts 时必须说明"检查不可用"，不能静默当成"没冲突"
  canned["/api/rag/search"] = {
    scope: "全部 SOP", conflict_check: "unavailable", conflicts: [],
    hits: [{ chunk_id: "sop-replenish#c0ffee00", doc_id: "sop-replenish", title: "补货 SOP",
             section: "口径", text: "目标覆盖天数 = 交期 + 21 天", score: 0.4,
             meta: { department: "通用", authority: "general" } }],
  };
  ragInput.value = "补货覆盖天数";
  await ctx.ragSearch();
  const ragHtml2 = String(ragResults.innerHTML);
  check("冲突检查不可用时有明确提示", ragHtml2.includes("冲突检查不可用"), ragHtml2.slice(0, 80));
  check("不可用时仍正常展示命中", ragHtml2.includes("补货 SOP"));

  // 同一文档内部前后矛盾：只报"语料不一致"，不能出现"采用…不采用…"的取舍结论
  canned["/api/rag/search"] = {
    scope: "全部 SOP", conflict_check: "none", conflicts: [{
      kind: "intra_doc", key: "clearance_trigger_days", doc_id: "sop-clearance",
      title: "清仓 SOP", values: ["60 天", "45 天"],
      reason: "同一文档对同一口径声明了多个取值，需修订语料",
    }],
    hits: [{ chunk_id: "sop-clearance#c0de0001", doc_id: "sop-clearance", title: "清仓 SOP",
             section: "触发条件", text: "可售天数 ≥ 60 天", score: 0.4,
             meta: { department: "通用", authority: "general" } }],
  };
  ragInput.value = "清仓触发天数";
  await ctx.ragSearch();
  const ragHtml3 = String(ragResults.innerHTML);
  check("语料内部不一致单独提示", ragHtml3.includes("语料内部不一致"), ragHtml3.slice(0, 80));
  check("内部不一致不给取舍结论", !ragHtml3.includes("不采用") && !ragHtml3.includes("采用 <b>"),
        ragHtml3.slice(0, 120));

  // none + 单一来源：必须说明"未做交叉验证"，不能让用户以为制度已核对过
  canned["/api/rag/search"] = {
    scope: "女装部门内部 SOP + 通用 SOP", conflict_check: "none", conflicts: [],
    conflict_diagnostics: { compared_keys: ["main_size_share"], single_source_keys: ["main_size_share"],
                            suppressed: 3, candidates: 30 },
    hits: [{ chunk_id: "sop-sizecurve#d0d0d0d0", doc_id: "sop-sizecurve", title: "尺码曲线与断码补全 SOP",
             section: "核心概念", text: "主销码：占比 ≥ 15% 的尺码", score: 0.42,
             meta: { department: "通用", authority: "governance" } }],
  };
  ragInput.value = "主销码 占比";
  await ctx.ragSearch();
  const ragHtml4 = String(ragResults.innerHTML);
  check("none 时说明单一来源", ragHtml4.includes("仅有单一来源"), ragHtml4.slice(0, 100));
  check("none 时点出未交叉验证", ragHtml4.includes("未做交叉验证"), ragHtml4.slice(0, 120));

  // 范围说明 + 部门例外：面板必须展示（后端早已返回，之前面板没渲染）
  canned["/api/rag/search"] = {
    scope: "女装部门内部 SOP + 通用 SOP", conflict_check: "none", conflicts: [],
    scope_notice: "本次问句涉及多个部门，其中「男装」不在当前账号可见范围内",
    department_overrides: [{ key: "clearance_trigger_days", doc_id: "sop-clearance-women",
                             title: "女装部门清仓 SOP", department: "女装", display: "45 天",
                             version: "v0.9", authority: "dept", authority_label: "部门细则" }],
    conflict_diagnostics: { compared_keys: ["clearance_trigger_days"], single_source_keys: [],
                            suppressed: 2, candidates: 30 },
    hits: [{ chunk_id: "sop-clearance#beef0002", doc_id: "sop-clearance", title: "清仓 SOP",
             section: "触发条件", text: "可售天数 ≥ 60 天", score: 0.4,
             meta: { department: "通用", authority: "general" } }],
  };
  ragInput.value = "女装和男装的清仓触发天数";
  await ctx.ragSearch();
  const ragHtml5 = String(ragResults.innerHTML);
  check("面板渲染检索范围说明", ragHtml5.includes("检索范围说明"), ragHtml5.slice(0, 90));
  check("范围说明点出看不见的部门", ragHtml5.includes("男装"));
  check("面板渲染部门例外", ragHtml5.includes("部门例外") && ragHtml5.includes("若问的是"),
        ragHtml5.slice(0, 160));
  check("部门例外不给取舍结论", !ragHtml5.includes("采用 <b>"), ragHtml5.slice(0, 160));

  // 未单独规定口径的部门：必须写明"按通用口径执行"，不能让用户以为它也是例外里的那个值
  canned["/api/rag/search"] = {
    scope: "全部 SOP", conflict_check: "none", conflicts: [],
    scope_notice: "这是**跨部门比较**的问题：当前只按全部部门 + 通用口径作答",
    department_overrides: [{ key: "clearance_trigger_days", doc_id: "sop-clearance-women",
                             title: "女装部门清仓 SOP", department: "女装", display: "45 天",
                             version: "v0.9", authority: "dept", authority_label: "部门细则" }],
    uncovered_departments: ["男装"],
    conflict_diagnostics: { compared_keys: ["clearance_trigger_days"], single_source_keys: [],
                            suppressed: 1, candidates: 30 },
    hits: [{ chunk_id: "sop-clearance#beef0003", doc_id: "sop-clearance", title: "清仓 SOP",
             section: "触发条件", text: "可售天数 ≥ 60 天", score: 0.4,
             meta: { department: "通用", authority: "general" } }],
  };
  ragInput.value = "哪个部门清仓门槛更低";
  await ctx.ragSearch();
  const ragHtml6 = String(ragResults.innerHTML);
  check("未覆盖部门写明按通用执行", ragHtml6.includes("未单独规定的部门") && ragHtml6.includes("男装"),
        ragHtml6.slice(0, 200));

  console.log("== 20. 流式增量撤回（delta_retract，A4） ==");
  // 模型「先说话、后决定调工具」时，服务端会发 delta_retract 撤回那段过程文字；
  // 前端必须清空已渲染的气泡内容，否则用户会把过程当答案。
  ctx.resetChatState("");
  ctx.appendStreamDelta("我先查一下库存");
  const bubble20 = chatMessages.children[chatMessages.children.length - 1];
  check("增量先渲染进气泡", String(bubble20.textContent).includes("我先查一下"), bubble20.textContent);
  const stages20 = [];
  ctx.handleStreamEvent("delta_retract", { reason: "tool_call", chars: 8 },
                        { setStage: (stage, text) => stages20.push(String(text || stage)) });
  check("撤回后气泡清空", String(bubble20.textContent) === "", bubble20.textContent);
  check("撤回有进度提示", stages20.some((t) => t.includes("撤回")), stages20);
  ctx.appendStreamDelta("可用库存 693 件");
  check("撤回后仍能继续写最终答案", String(bubble20.textContent).includes("693"), bubble20.textContent);

  console.log("== 21. 工具名不可用（AGENT_INVALID_TOOL_NAME，A3） ==");
  // 服务端对「模型点了不存在的工具名」返回 fatal 的 SSE error：
  // 前端不能再自动降级 POST —— 那等于把同一轮再跑一遍（双倍 token，还是同一个错）。
  const realFetch21 = sandbox.fetch;
  const seenUrls = [];
  sandbox.fetch = async (url, opts) => {
    seenUrls.push(String(url));
    if (String(url).includes("/api/chat/stream")) {
      return sseResponse([
        'event: error\ndata: ' + JSON.stringify({
          message: "模型调用了不存在的工具：no_such_tool（错误码 AGENT_INVALID_TOOL_NAME｜追踪 t_tool9）",
          status: 422, error_code: "AGENT_INVALID_TOOL_NAME", trace_id: "t_tool9",
          fatal: true, retryable: true,
        }) + '\n\n',
      ]);
    }
    return realFetch21(url, opts);
  };
  const progress21 = {
    messages: [],
    setStage(stage, text) { this.messages.push(String(text || stage)); },
    fail(message) { this.messages.push(String(message || "fail")); },
    addLlmStart() {}, addLlmEnd() {}, addStepStart() {},
    completeStep() {}, setServerElapsed() {},
  };
  let fatalErr = null;
  const callsBefore21 = calls.length;
  try {
    await ctx.requestChat("看下 ST-01-C1-M", progress21);
  } catch (err) {
    fatalErr = err;
  }
  sandbox.fetch = realFetch21;
  check("fatal 错误会抛给上层（不吞掉）", !!fatalErr, fatalErr && fatalErr.message);
  check("错误码被透传", !!fatalErr && fatalErr.code === "AGENT_INVALID_TOOL_NAME",
        fatalErr && fatalErr.code);
  check("进度条展示了可读原因", progress21.messages.some((t) => t.includes("不存在的工具")),
        progress21.messages);
  const postedAfter21 = calls.slice(callsBefore21).filter((c) => c.path === "/api/chat");
  check("fatal 之后没有降级 POST（不重跑同一轮）", postedAfter21.length === 0, postedAfter21);

  console.log("== 22. 顶栏存储徽标：关系库 + 向量库（Qdrant 也属于存储层）==");
  // 只写 h.database 会把 Qdrant 藏起来：用户看到「mysql」会以为没有向量库。
  canned["/api/health"] = Object.assign({}, canned["/api/health"], {
    database: "mysql",
    vector_store: { backend: "qdrant", mode: "server", collection: "kb_chunks_512",
                    dim: 512, points: 67, ok: true, degraded: false },
  });
  await ctx.loadHealth();
  check("存储徽标 = 关系库、向量库", String(badgeDb.textContent) === "mysql、qdrant", badgeDb.textContent);
  check("存储徽标 tooltip 给出集合/维度/点数",
        String(badgeDb.title).includes("kb_chunks_512") && String(badgeDb.title).includes("512 维")
        && String(badgeDb.title).includes("67 点"), badgeDb.title);
  check("memory 模式照实写 memory（不冒充向量库）",
        ctx.storageLabel({ database: "sqlite", vector_store: { backend: "memory", degraded: true } })
        === "sqlite、memory");
  check("旧版 health（无 vector_store）只显示关系库",
        ctx.storageLabel({ database: "sqlite" }) === "sqlite");
  check("降级原因写进 tooltip",
        String(ctx.storageTitle({ database: "mysql", vector_store: { backend: "qdrant", degraded: true,
                                                                    degraded_reason: "Qdrant 不可用" } }))
        .includes("Qdrant 不可用"));

  console.log(failures.length ? `\n${failures.length} 项失败: ${failures.join(", ")}` : "\n全部通过");
  process.exit(failures.length ? 1 : 0);
})().catch((err) => {
  console.error("smoke crashed:", err && err.stack || err);
  process.exit(1);
});
