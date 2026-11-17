/* ============================================================
 * 生图 Agent 管理员面板（/image_admin.js）
 * 只用原生 DOM + 手绘 SVG（零外部库，CSP 'self' 兼容）。
 * 依赖 image.js 暴露的 window.ImageApp（api / esc / authHeaders）。
 * ============================================================ */

const ImageAdmin = (() => {
  const SVG_NS = "http://www.w3.org/2000/svg";
  const CHART_W = 760;
  const CHART_H = 260;
  const PAD = { left: 44, right: 16, top: 18, bottom: 34 };

  const app = () => window.ImageApp || {};
  const esc = (value) => (app().esc ? app().esc(value) : String(value ?? ""));
  const api = (path, options) => app().api(path, options);

  /* ------------------------------------------------------------ 小工具 */
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value === undefined || value === null) return;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    });
    (children || []).forEach((child) => node.appendChild(child));
    return node;
  }

  function svg(tag, attrs, children) {
    const node = document.createElementNS(SVG_NS, tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value !== undefined && value !== null) node.setAttribute(key, value);
    });
    (children || []).forEach((child) => node.appendChild(child));
    return node;
  }

  function fmt(value, digits) {
    const number = Number(value || 0);
    if (digits === undefined) return String(Math.round(number));
    return number.toFixed(digits);
  }

  function shortKey(key, granularity) {
    if (!key) return "-";
    if (granularity === "hour") return key.slice(5).replace("T", " ");
    if (granularity === "month") return key;
    if (granularity === "week") return key.replace("-W", " W");
    return key.slice(5);
  }

  /* ------------------------------------------------------------ 指标卡 */
  function renderCards(container, cards, extra) {
    if (!container) return;
    const data = cards || {};
    const outbox = (extra && extra.outbox) || {};
    const items = [
      { label: "RAG 缓存命中率", value: `${fmt((data.cache_hit_rate || 0) * 100, 1)}%`,
        hint: "知识库召回的进程内缓存" },
      { label: "LLM 调用 / token", value: `${fmt(data.llm_calls)} / ${fmt(data.llm_tokens)}`,
        hint: "生图链路当前不调 LLM（rubric 预留）" },
      { label: "当前生成中图片", value: fmt(data.in_flight_images), hint: "调度器在途" },
      { label: "累计交付 / 尝试", value: `${fmt(data.images_delivered)} / ${fmt(data.images_attempted)}`,
        hint: `任务 ${fmt(data.tasks_done)} 完成 · ${fmt(data.tasks_failed)} 失败` },
      { label: "首轮通过率", value: `${fmt((data.first_pass_rate || 0) * 100, 1)}%`,
        hint: `成本 ¥${fmt(data.cost_cny, 2)} · 回写待重放 ${fmt(outbox.pending)}` },
    ];
    container.innerHTML = items.map((item) => `
      <div class="card">
        <div class="label">${esc(item.label)}</div>
        <div class="value">${esc(item.value)}</div>
        <div class="hint">${esc(item.hint)}</div>
      </div>
    `).join("");
  }

  /* ------------------------------------------------------------ 折线图 */
  function chartPoints(buckets, field, max) {
    const innerW = CHART_W - PAD.left - PAD.right;
    const innerH = CHART_H - PAD.top - PAD.bottom;
    const step = buckets.length > 1 ? innerW / (buckets.length - 1) : 0;
    return buckets.map((bucket, index) => {
      const value = Number(bucket[field] || 0);
      const x = PAD.left + step * index;
      const y = PAD.top + innerH - (max ? (value / max) * innerH : 0);
      return { x, y, value, bucket };
    });
  }

  function polyline(points, color, dashed) {
    const d = points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
    return svg("path", {
      d, fill: "none", stroke: color, "stroke-width": dashed ? 1.4 : 2,
      "stroke-dasharray": dashed ? "4 4" : null, "stroke-linejoin": "round",
      "stroke-linecap": "round",
    });
  }

  function renderChart(container, payload) {
    if (!container) return;
    const buckets = (payload && payload.buckets) || [];
    const granularity = (payload && payload.granularity) || "day";
    container.innerHTML = "";
    if (!buckets.length) {
      container.appendChild(el("div", { class: "image-chart-empty", text: "暂无数据（跑几个任务再刷新）" }));
      return;
    }
    const max = Math.max(1, ...buckets.map((bucket) => Number(bucket.images || 0)),
      ...buckets.map((bucket) => Number(bucket.images_attempted || 0)));
    const gridY = 4;
    const innerH = CHART_H - PAD.top - PAD.bottom;

    const svgNode = svg("svg", {
      viewBox: `0 0 ${CHART_W} ${CHART_H}`, role: "img", "aria-label": "生图指标折线图",
    });

    // 横向网格 + y 轴刻度
    for (let i = 0; i <= gridY; i += 1) {
      const y = PAD.top + (innerH / gridY) * i;
      svgNode.appendChild(svg("line", {
        x1: PAD.left, y1: y, x2: CHART_W - PAD.right, y2: y,
        stroke: "rgba(34,48,66,0.8)", "stroke-width": 1,
      }));
      const value = Math.round((max / gridY) * (gridY - i));
      svgNode.appendChild(svg("text", {
        x: PAD.left - 8, y: y + 4, "text-anchor": "end", "font-size": 10, fill: "#8fa3b8",
      }, [document.createTextNode(String(value))]));
    }

    const delivered = chartPoints(buckets, "images", max);
    const attempted = chartPoints(buckets, "images_attempted", max);
    svgNode.appendChild(polyline(attempted, "#8fa3b8", true));
    svgNode.appendChild(polyline(delivered, "#38bdf8", false));

    delivered.forEach((point) => {
      svgNode.appendChild(svg("circle", {
        cx: point.x, cy: point.y, r: 2.6, fill: "#38bdf8",
      }));
    });

    // x 轴标签（最多 8 个，避免重叠）
    const stride = Math.max(1, Math.ceil(buckets.length / 8));
    buckets.forEach((bucket, index) => {
      if (index % stride !== 0 && index !== buckets.length - 1) return;
      const point = delivered[index];
      svgNode.appendChild(svg("text", {
        x: point.x, y: CHART_H - PAD.bottom + 18, "text-anchor": "middle",
        "font-size": 10, fill: "#8fa3b8",
      }, [document.createTextNode(shortKey(bucket.key, granularity))]));
    });

    // 悬停热区 + tooltip
    const tip = el("div", { class: "image-chart-tip", hidden: "hidden" });
    const step = buckets.length > 1
      ? (CHART_W - PAD.left - PAD.right) / (buckets.length - 1)
      : CHART_W - PAD.left - PAD.right;
    buckets.forEach((bucket, index) => {
      const x = PAD.left + step * index;
      const hot = svg("rect", {
        x: x - step / 2, y: PAD.top, width: Math.max(6, step), height: innerH,
        fill: "transparent", "data-index": index,
      });
      hot.addEventListener("mousemove", (event) => {
        const rows = Object.entries(bucket.by_user || {}).map(
          ([user, stats]) => `${user}：交付 ${stats.images} / 尝试 ${stats.images_attempted}`
        );
        tip.innerHTML = [
          `<b>${esc(shortKey(bucket.key, granularity))}</b>`,
          `交付 ${bucket.images} · 尝试 ${bucket.images_attempted}`,
          `任务 ${bucket.tasks} · 失败 ${bucket.failed}`,
          rows.length ? rows.join("\n") : "（无账户细分）",
        ].map((line) => esc(line)).join("<br/>");
        tip.hidden = false;
        const box = container.getBoundingClientRect();
        tip.style.left = `${event.clientX - box.left}px`;
        tip.style.top = `${event.clientY - box.top}px`;
      });
      hot.addEventListener("mouseleave", () => {
        tip.hidden = true;
      });
      svgNode.appendChild(hot);
    });

    const box = el("div", { class: "image-chart-box" }, [svgNode, tip]);
    container.appendChild(box);
    container.appendChild(el("div", {
      class: "image-chart-empty",
      text: `实线 = 交付张数，虚线 = 尝试张数；粒度 ${granularity}，共 ${buckets.length} 个周期`,
    }));
  }

  /* ------------------------------------------------------------ 表格 */
  function renderTasks(container, tasks) {
    if (!container) return;
    if (!tasks || !tasks.length) {
      container.innerHTML = '<tr><td colspan="6" class="muted">暂无任务</td></tr>';
      return;
    }
    container.innerHTML = tasks.slice(0, 30).map((task) => `
      <tr>
        <td><code>${esc(task.task_id)}</code></td>
        <td>${esc(task.username || "-")}</td>
        <td><span class="tag ${task.status === "done" ? "low" : task.status === "failed" ? "high" : "gray"}">${esc(task.status)}</span></td>
        <td>${esc(task.attempts)}</td>
        <td>${esc(task.delivered_images)}</td>
        <td>${esc(task.backfill_status || "-")}</td>
      </tr>
    `).join("");
  }

  function renderQuota(container, quotas) {
    if (!container) return;
    if (!quotas || !quotas.length) {
      container.innerHTML = '<tr><td colspan="5" class="muted">暂无配额记录（首次出图后生成）</td></tr>';
      return;
    }
    container.innerHTML = quotas.map((row) => `
      <tr>
        <td>${esc(row.username)}</td>
        <td>${row.period_type === "unlimited" ? '<span class="tag low">不限</span>' : "普通"}</td>
        <td>${esc(row.period_type)}</td>
        <td>${row.period_type === "unlimited" ? "∞" : esc(row.period_limit)}</td>
        <td>${esc(row.used)}</td>
      </tr>
    `).join("");
  }

  /* ------------------------------------------------------------ 刷新 */
  async function refresh(state) {
    const granularity = (document.getElementById("metric-granularity") || {}).value || "day";
    const username = ((document.getElementById("metric-username") || {}).value || "").trim();
    const query = `granularity=${granularity}&count=30${username ? `&username=${encodeURIComponent(username)}` : ""}`;

    const metrics = await api(`/api/image/admin/metrics?${query}`);
    renderCards(document.getElementById("metric-cards"), metrics.cards, metrics);
    renderChart(document.getElementById("image-chart"), metrics);

    const taskQuery = username ? `?username=${encodeURIComponent(username)}&limit=30` : "?limit=30";
    const tasks = await api(`/api/image/admin/tasks${taskQuery}`);
    renderTasks(document.getElementById("admin-task-rows"), tasks.tasks);

    const quota = await api("/api/image/admin/quota");
    renderQuota(document.getElementById("admin-quota-rows"), quota.quotas);
    if (state && state.user) {
      document.body.dataset.admin = state.user.username;
    }
  }

  return { refresh, renderChart, renderCards, renderTasks, renderQuota, CHART_W, CHART_H, PAD };
})();

window.ImageAdmin = ImageAdmin;
