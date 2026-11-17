/* ============================================================
 * 生图 Agent 前端（/image.html 专用，不加载 app.js）
 * 约定：
 *  - 独立登录态：localStorage.image_agent_token（与 SKU 页互不影响）
 *  - 参数只从中台来：商品清单 / 构图 / 场景风格 / 张数 / 白名单都在 /products 与 /catalog
 *  - 用户不能上传图片：本文件里没有上传逻辑，也没有 <input type=file>
 *  - 进度走 SSE：/api/image/tasks/{id}/stream
 * ============================================================ */

const IMAGE_BUILD = "image-v1";
const TOKEN_KEY = "image_agent_token";
const USER_KEY = "image_agent_user";
const MAX_BATCH = 5;

const state = {
  token: "",
  user: null,
  products: [],
  catalog: null,
  selected: new Set(),
  streams: new Map(),
};

/* ---------------------------------------------------------------- 基础工具 */
function $(id) {
  return document.getElementById(id);
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = value;
}

function setHint(selector, text, tone) {
  const el = typeof selector === "string" ? $(selector) : selector;
  if (!el) return;
  el.textContent = text || "";
  el.classList.remove("tag", "high", "mid", "low", "gray");
  if (tone) el.classList.add("tag", tone);
}

function authHeaders(extra) {
  const headers = Object.assign({}, extra || {});
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  return headers;
}

async function api(path, options) {
  const opts = Object.assign({}, options || {});
  opts.headers = authHeaders(opts.headers);
  if (opts.json !== undefined) {
    opts.method = opts.method || "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
    delete opts.json;
  }
  const resp = await fetch(path, opts);
  let body = null;
  const type = resp.headers.get("content-type") || "";
  if (type.includes("application/json")) {
    body = await resp.json().catch(() => null);
  }
  if (!resp.ok) {
    const code = (body && body.error_code) || resp.headers.get("X-Error-Code") || `HTTP ${resp.status}`;
    const detail = (body && (body.detail || body.message)) || resp.statusText || "";
    const err = new Error(`${code}${detail ? "：" + detail : ""}`);
    err.code = code;
    err.status = resp.status;
    err.retryable = Boolean(body && body.retryable);
    throw err;
  }
  return body;
}

/* ---------------------------------------------------------------- 登录 */
function loadSession() {
  try {
    state.token = localStorage.getItem(TOKEN_KEY) || "";
    state.user = JSON.parse(localStorage.getItem(USER_KEY) || "null");
  } catch (err) {
    state.token = "";
    state.user = null;
  }
}

function saveSession(token, user) {
  state.token = token || "";
  state.user = user || null;
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
  if (user) localStorage.setItem(USER_KEY, JSON.stringify(user));
  else localStorage.removeItem(USER_KEY);
}

function renderIdentity() {
  const loggedIn = Boolean(state.token && state.user);
  $("login-panel").hidden = loggedIn;
  $("workbench").hidden = !loggedIn;
  $("admin-panel").hidden = !(loggedIn && state.user && state.user.is_admin);
  $("btn-logout").hidden = !loggedIn;
  setText("badge-user-value", loggedIn ? `${state.user.username}（${state.user.role}）` : "未登录");
  if (loggedIn) {
    setText("login-hint", "");
    loadQuota();
    loadProducts();
    loadCatalog();
    if (state.user.is_admin) loadAdmin();
  }
}

async function doLogin() {
  const username = ($("login-username").value || "").trim();
  const password = $("login-password").value || "";
  if (!username || !password) {
    setText("login-hint", "账号和密码都要填");
    return;
  }
  try {
    const body = await api("/api/image/auth/login", { json: { username, password } });
    saveSession(body.token, body.user);
    $("login-password").value = "";
    setText("login-hint", body.secret_is_default ? "提示：IMAGE_AUTH_SECRET 仍是默认值（上线前改掉）" : "");
    renderIdentity();
  } catch (err) {
    setText("login-hint", err.message);
  }
}

async function doLogout() {
  try {
    await api("/api/image/auth/logout", { method: "POST" });
  } catch (err) {
    /* 令牌可能已过期，本地照样清 */
  }
  saveSession("", null);
  state.selected.clear();
  state.products = [];
  renderIdentity();
}

/* ---------------------------------------------------------------- 商品与参数（中台） */
async function loadProducts() {
  if (!state.token) return;
  const keyword = ($("product-keyword").value || "").trim();
  try {
    const body = await api(`/api/image/products?keyword=${encodeURIComponent(keyword)}&limit=50`);
    state.products = body.products || [];
    renderProducts();
    setText("badge-products-value", `${state.products.length} 个商品`);
  } catch (err) {
    $("product-rows").innerHTML = `<tr><td colspan="5" class="muted">商品清单加载失败：${esc(err.message)}</td></tr>`;
    setText("badge-products-value", err.code || "失败");
  }
}

function renderProducts() {
  const rows = $("product-rows");
  if (!rows) return;
  if (!state.products.length) {
    rows.innerHTML = '<tr><td colspan="5" class="muted">没有商品（换关键词再试，或检查中台是否可用）</td></tr>';
    return;
  }
  rows.innerHTML = state.products.map((item) => {
    const checked = state.selected.has(item.product_id) ? "checked" : "";
    const disabled = !state.selected.has(item.product_id) && state.selected.size >= MAX_BATCH;
    return [
      `<tr class="${checked ? "selected" : ""}">`,
      `<td><input type="checkbox" class="product-check" data-product="${esc(item.product_id)}" ${checked} ${disabled ? "disabled" : ""} /></td>`,
      `<td>${esc(item.title)}</td>`,
      `<td>${esc(item.category)}</td>`,
      `<td><code>${esc(item.product_id)}</code></td>`,
      `<td>${item.main_image_id ? '<span class="tag low">有</span>' : '<span class="tag high">无</span>'}</td>`,
      "</tr>",
    ].join("");
  }).join("");
  rows.querySelectorAll(".product-check").forEach((box) => {
    box.addEventListener("change", () => {
      const productId = box.dataset.product;
      if (box.checked) {
        if (state.selected.size >= MAX_BATCH) {
          box.checked = false;
          setText("create-hint", `最多选 ${MAX_BATCH} 个商品`);
          return;
        }
        state.selected.add(productId);
      } else {
        state.selected.delete(productId);
      }
      renderProducts();
      updateCreateHint();
    });
  });
  updateCreateHint();
}

function updateCreateHint() {
  const count = state.selected.size;
  setText("create-hint", count ? `已选 ${count}/${MAX_BATCH} 个商品` : "先勾选商品");
  const button = $("btn-create");
  if (button) button.disabled = count === 0;
}

async function loadCatalog() {
  if (!state.token) return;
  try {
    const body = await api("/api/image/catalog?platform=淘系");
    state.catalog = body;
    const compositions = $("composition-select");
    const styles = $("scene-style-select");
    compositions.innerHTML = '<option value="">中台默认</option>' + (body.compositions || []).map(
      (item) => `<option value="${esc(item.preset_id)}">${esc(item.label)}</option>`
    ).join("");
    styles.innerHTML = '<option value="">中台默认</option>' + (body.scene_styles || []).map(
      (item) => `<option value="${esc(item.preset_id)}">${esc(item.label)}</option>`
    ).join("");
    const rules = body.platform_rules || {};
    const aspects = ["1:1", "3:4", "4:3", "16:9"];
    $("aspect-select").innerHTML = aspects.map((item) => (
      `<option value="${item}" ${item === (rules.aspect || "1:1") ? "selected" : ""}>${item}</option>`
    )).join("");
    const policy = body.plan_policy || {};
    $("count-input").value = policy.default_count || 6;
    $("count-input").max = policy.max_count || 9;
  } catch (err) {
    setText("create-hint", `中台参数加载失败：${err.message}`);
  }
}

async function loadQuota() {
  try {
    const body = await api("/api/image/quota");
    if (body.period_type === "unlimited") {
      setHint("quota-badge", "配额：管理员不限", "low");
    } else {
      setHint("quota-badge", `配额：剩余 ${body.remaining}/${body.limit}（${body.period_type}）`,
        body.remaining > 0 ? "low" : "high");
    }
  } catch (err) {
    setHint("quota-badge", `配额读取失败：${err.code || err.message}`, "high");
  }
}

/* ---------------------------------------------------------------- 建任务 + SSE */
async function createTasks() {
  const productIds = Array.from(state.selected);
  if (!productIds.length) {
    setText("create-hint", "先勾选商品");
    return;
  }
  const prompt = ($("prompt-input").value || "").trim();
  if (!prompt) {
    setText("create-hint", "请填写需求描述");
    return;
  }
  const payload = {
    product_ids: productIds,
    prompt,
    aspect: $("aspect-select").value || "1:1",
    n: Number($("count-input").value || 6),
  };
  const composition = $("composition-select").value;
  const style = $("scene-style-select").value;
  if (composition) payload.compositions = [composition];
  if (style) payload.scene_styles = [style];

  const button = $("btn-create");
  button.disabled = true;
  setText("create-hint", "提交中…");
  try {
    const body = await api("/api/image/tasks", { json: payload });
    setText("batch-badge", `batch: ${body.batch_id}`);
    setText("create-hint", `已创建 ${body.tasks.length} 个任务`);
    body.tasks.forEach((task) => watchTask(task.task_id, task.product_id));
    loadQuota();
  } catch (err) {
    setText("create-hint", err.retryable ? `${err.message}（可重试）` : err.message);
  } finally {
    button.disabled = false;
  }
}

function parseSseChunk(chunk) {
  const lines = chunk.split("\n");
  let event = "message";
  const dataLines = [];
  lines.forEach((line) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  });
  if (!dataLines.length) return null;
  let data = dataLines.join("\n");
  try {
    data = JSON.parse(data);
  } catch (err) {
    /* 非 JSON 数据按原文交给调用方 */
  }
  return { event, data };
}

/* SSE 用 fetch 读流（带 Authorization 头），而不是 EventSource：
 * EventSource 不能加请求头，只能把令牌放 URL——那会漏进日志与 Referer。 */
async function openStream(taskId, handlers) {
  const controller = new AbortController();
  let resp;
  try {
    resp = await fetch(`/api/image/tasks/${taskId}/stream`, {
      headers: authHeaders({ Accept: "text/event-stream" }),
      signal: controller.signal,
    });
  } catch (err) {
    handlers.onError && handlers.onError(err);
    return controller;
  }
  if (!resp.ok || !resp.body) {
    handlers.onError && handlers.onError(new Error("stream HTTP " + resp.status));
    return controller;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  (async () => {
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let index = buffer.indexOf("\n\n");
        while (index >= 0) {
          const chunk = buffer.slice(0, index);
          buffer = buffer.slice(index + 2);
          const parsed = parseSseChunk(chunk);
          if (parsed) handlers.onEvent && handlers.onEvent(parsed.event, parsed.data);
          index = buffer.indexOf("\n\n");
        }
      }
    } catch (err) {
      if (err.name !== "AbortError" && handlers.onError) handlers.onError(err);
    } finally {
      handlers.onClose && handlers.onClose();
    }
  })();
  return controller;
}

function watchTask(taskId, productId) {
  if (state.streams.has(taskId)) return;
  const container = $("progress");
  const row = document.createElement("div");
  row.className = "progress-item running";
  row.innerHTML = [
    `<span class="pi-task">${esc(taskId)}</span>`,
    `<span class="tag gray">${esc(productId || "-")}</span>`,
    '<span class="pi-status">running…</span>',
    '<span class="pi-name muted"></span>',
  ].join("");
  container.appendChild(row);
  const statusEl = row.querySelector(".pi-status");
  const nameEl = row.querySelector(".pi-name");

  const controller = openStream(taskId, {
    onEvent(event, data) {
      if (event === "snapshot") {
        nameEl.textContent = data.product_id ? `商品 ${data.product_id}` : "";
      } else if (event === "stage") {
        statusEl.textContent = data.status;
        row.className = "progress-item " + data.status;
      } else if (event === "asset") {
        appendGallery(taskId, data);
      } else if (event === "backfill") {
        row.dataset.backfill = data.status;
        nameEl.textContent = `回写中台：${data.status}`;
      } else if (event === "done") {
        statusEl.textContent = data.status + (data.timed_out ? "（等待超时）" : "");
        row.className = "progress-item " + data.status;
        if (data.error_code) nameEl.textContent = data.error_code;
        loadGallery(taskId);
        loadQuota();
      } else if (event === "fatal") {
        statusEl.textContent = "fatal";
        nameEl.textContent = data.message || data.error_code || "";
        row.className = "progress-item failed";
      }
    },
    onError() {
      statusEl.textContent = "连接断开";
    },
    onClose() {
      state.streams.delete(taskId);
    },
  });
  state.streams.set(taskId, controller);
}

/* ---------------------------------------------------------------- 画廊 */
function appendGallery(taskId, asset) {
  let card = document.querySelector(`.gallery-card[data-asset="${asset.asset_id}"]`);
  if (card) return;
  const gallery = $("gallery");
  card = document.createElement("figure");
  card.className = "gallery-card";
  card.dataset.asset = asset.asset_id;
  card.dataset.task = taskId;
  card.innerHTML = [
    `<div class="gc-head"><span>slot ${esc(asset.slot)}</span><span class="muted">${esc(asset.kind)}</span></div>`,
    `<img loading="lazy" alt="slot ${esc(asset.slot)}" src="${esc(asset.url)}" />`,
    `<figcaption class="gc-foot"><span>${esc(asset.width)}×${esc(asset.height)} · ${esc(asset.mime)}</span>`,
    `<code>${esc(asset.sha256 ? asset.sha256.slice(0, 16) : "")}…</code></figcaption>`,
  ].join("");
  card.querySelector("img").addEventListener("error", () => {
    card.querySelector("img").alt = "图片需要登录后查看";
  });
  gallery.appendChild(card);
  // 图片接口需要 Authorization 头：EventSource 带不了，这里用 blob 方式取一次
  fetch(asset.url, { headers: authHeaders() })
    .then((resp) => (resp.ok ? resp.blob() : null))
    .then((blob) => {
      if (blob) card.querySelector("img").src = URL.createObjectURL(blob);
    })
    .catch(() => { /* 保留占位 alt */ });
}

async function loadGallery(taskId) {
  try {
    const detail = await api(`/api/image/tasks/${taskId}`);
    (detail.assets || []).forEach((asset) => appendGallery(taskId, asset));
    if ((detail.refs || []).length) {
      const card = document.querySelector(`.gallery-card[data-task="${taskId}"]`);
      if (card && !card.querySelector(".gallery-refs")) {
        const refs = document.createElement("div");
        refs.className = "gallery-refs";
        refs.innerHTML = detail.refs.map((ref) => `<img alt="参考图" src="${esc(ref.url)}" />`).join("");
        card.appendChild(refs);
      }
    }
  } catch (err) {
    /* 详情拉取失败不影响已有画廊 */
  }
}

/* ---------------------------------------------------------------- 管理员面板（委托给 image_admin.js） */
async function loadAdmin() {
  if (!window.ImageAdmin) return;
  try {
    await window.ImageAdmin.refresh(state);
  } catch (err) {
    setText("create-hint", `管理面板加载失败：${err.message}`);
  }
}

/* ---------------------------------------------------------------- 健康检查 */
async function loadHealth() {
  try {
    const resp = await fetch("/api/image/health");
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const body = await resp.json();
    setText("badge-service-value", body.status === "ok" ? "正常" : String(body.status || "-"));
    setText("badge-provider", body.provider || "-");
    const source = body.product_source === "mcp" ? "中台 MCP" : "本地替身";
    setText("badge-products-value", source);
  } catch (err) {
    setText("badge-service-value", "离线");
  }
}

/* ---------------------------------------------------------------- 初始化 */
function bindEvents() {
  $("btn-login").addEventListener("click", doLogin);
  $("login-password").addEventListener("keydown", (event) => {
    if (event.key === "Enter") doLogin();
  });
  $("btn-logout").addEventListener("click", doLogout);
  $("btn-products").addEventListener("click", loadProducts);
  $("product-keyword").addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadProducts();
  });
  $("btn-create").addEventListener("click", createTasks);
  $("btn-clear").addEventListener("click", () => {
    state.selected.clear();
    renderProducts();
  });
  const metricsButton = $("btn-metrics");
  if (metricsButton) {
    metricsButton.addEventListener("click", () => {
      if (window.ImageAdmin) window.ImageAdmin.refresh(state);
    });
  }
  const replayButton = $("btn-replay-outbox");
  if (replayButton) {
    replayButton.addEventListener("click", async () => {
      try {
        const body = await api("/api/image/admin/replay-outbox", { method: "POST" });
        setText("create-hint", `重放完成：sent=${body.sent} failed=${body.failed}`);
        if (window.ImageAdmin) window.ImageAdmin.refresh(state);
      } catch (err) {
        setText("create-hint", err.message);
      }
    });
  }
  const granularity = $("metric-granularity");
  if (granularity) {
    granularity.addEventListener("change", () => {
      if (window.ImageAdmin) window.ImageAdmin.refresh(state);
    });
  }
}

function init() {
  loadSession();
  bindEvents();
  renderIdentity();
  loadHealth();
  setInterval(loadHealth, 15000);
  if (window.__imageBuild && window.__imageBuild !== IMAGE_BUILD) {
    console.warn("[image] 页面与脚本版本不一致，请强刷（Ctrl+F5）");
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}

window.ImageApp = { state, api, esc, authHeaders, IMAGE_BUILD, MAX_BATCH };
