/* ============================================================
 * 接口文档页脚本（`/api.html`）
 *
 * 为什么不直接用 FastAPI 自带的 `/docs`：
 *   那个页面从 cdn.jsdelivr.net 加载 Swagger UI 的 CSS/JS，而本项目的 CSP 是
 *   `default-src 'self'`（见 src/app/main.py 的安全响应头），外链会被浏览器拦掉 →
 *   打开是一片空白；就算放宽 CSP，受限网络下 CDN 也不一定可达。
 *
 * 这里的做法：取同源 `/openapi.json`，用原生 JS 渲染成分组可折叠列表（零外部依赖、
 * 与 about.html / markdown-lite.js 一致的路子）。分组按 OpenAPI tag，没有 tag 时按
 * `/api/<段>` 的第一段路径分组。
 *
 * 纯函数（groupEndpoints / filterGroups / renderGroups）挂在 `window.ApiDocs` 上，
 * 便于 node 侧单测直接调用，不用模拟浏览器。
 * ============================================================ */
(function () {
  "use strict";

  var HTTP_METHODS = ["get", "post", "put", "patch", "delete", "head", "options"];

  // 路径段 → 中文分组名（没有 tag 时的兜底）
  var GROUP_LABELS = {
    auth: "认证与权限",
    chat: "对话与运行详情",
    memory: "记忆（会话 / 档案 / 偏好）",
    actions: "人工确认（补货 / 售后）",
    rag: "知识库检索",
    patrol: "批量巡检",
    inspect: "单 SKU 研判",
    inspections: "研判记录",
    anomalies: "异常列表",
    skus: "SKU 数据",
    metrics: "指标与延迟",
    cache: "缓存明细",
    queue: "队列状态",
    health: "健康检查",
    scheduler: "定时调度",
    admin: "运维",
    agents: "Agent 目录",
  };

  function esc(value) {
    return String(value === undefined || value === null ? "" : value)
      .replace(/[&<>"']/g, function (ch) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
      });
  }

  function resolveRef(schema, spec) {
    if (!schema || typeof schema !== "object") return null;
    var ref = schema.$ref;
    if (typeof ref === "string" && ref.indexOf("#/") === 0) {
      var node = spec || {};
      ref.slice(2).split("/").forEach(function (part) {
        node = node ? node[part] : null;
      });
      return node || null;
    }
    return schema;
  }

  function typeOf(schema, spec) {
    var resolved = resolveRef(schema, spec);
    if (!resolved) return "any";
    if (resolved.oneOf || resolved.anyOf) return "oneOf";
    var type = resolved.type || (resolved.properties ? "object" : "any");
    if (type === "array") {
      return "array<" + typeOf(resolved.items, spec) + ">";
    }
    if (type === "string" && Array.isArray(resolved.enum) && resolved.enum.length) {
      return "enum(" + resolved.enum.join(" | ") + ")";
    }
    return type;
  }

  function groupOf(path, op) {
    var tags = op && op.tags;
    if (Array.isArray(tags) && tags.length) return String(tags[0]);
    var parts = String(path).split("/").filter(Boolean);
    var seg = parts[0] === "api" ? parts[1] : parts[0];
    return seg ? (GROUP_LABELS[seg] || seg) : "其它";
  }

  function summarizeOperation(method, path, op, spec) {
    var parameters = (op.parameters || []).map(function (param) {
      return {
        name: param.name,
        in: param.in || "",
        required: Boolean(param.required),
        type: typeOf(param.schema, spec),
        desc: param.description || "",
      };
    });
    var bodyFields = [];
    var content = op.requestBody && op.requestBody.content && op.requestBody.content["application/json"];
    if (content) {
      var schema = resolveRef(content.schema, spec) || {};
      var required = Array.isArray(schema.required) ? schema.required : [];
      Object.keys(schema.properties || {}).forEach(function (name) {
        var prop = schema.properties[name] || {};
        bodyFields.push({
          name: name,
          type: typeOf(prop, spec),
          required: required.indexOf(name) >= 0,
          desc: prop.description || "",
        });
      });
    }
    var responses = Object.keys(op.responses || {}).map(function (code) {
      var resp = op.responses[code] || {};
      return { code: code, desc: resp.description || "" };
    });
    return {
      method: String(method).toUpperCase(),
      path: path,
      summary: op.summary || "",
      description: op.description || "",
      params: parameters,
      bodyFields: bodyFields,
      responses: responses,
      deprecated: Boolean(op.deprecated),
    };
  }

  /** OpenAPI spec → [{name, operations:[...]}]，保持 spec 里 paths 的原始顺序。 */
  function groupEndpoints(spec) {
    var groups = [];
    var index = {};
    var paths = (spec && spec.paths) || {};
    Object.keys(paths).forEach(function (path) {
      var item = paths[path] || {};
      HTTP_METHODS.forEach(function (method) {
        if (!item[method]) return;
        var op = summarizeOperation(method, path, item[method], spec);
        var name = groupOf(path, item[method]);
        if (!index[name]) {
          index[name] = { name: name, operations: [] };
          groups.push(index[name]);
        }
        index[name].operations.push(op);
      });
    });
    return groups;
  }

  /** 关键词过滤（同时匹配分组名、方法、路径、摘要）；空关键词原样返回。 */
  function filterGroups(groups, keyword) {
    var q = String(keyword || "").trim().toLowerCase();
    if (!q) return groups;
    return groups
      .map(function (group) {
        return {
          name: group.name,
          operations: group.operations.filter(function (op) {
            var hay = [group.name, op.method, op.path, op.summary].join(" ").toLowerCase();
            return hay.indexOf(q) >= 0;
          }),
        };
      })
      .filter(function (group) {
        return group.operations.length > 0;
      });
  }

  function tableHtml(headers, rows) {
    if (!rows.length) return "";
    var head = headers.map(function (h) { return "<th>" + esc(h) + "</th>"; }).join("");
    var body = rows.map(function (cells) {
      return "<tr>" + cells.map(function (cell) { return "<td>" + cell + "</td>"; }).join("") + "</tr>";
    }).join("");
    return '<table class="api-table"><thead><tr>' + head + "</tr></thead><tbody>" + body + "</tbody></table>";
  }

  function operationHtml(op) {
    var methodClass = op.method.toLowerCase();
    var parts = [
      '<details class="api-op">',
      '<summary><span class="api-method ' + methodClass + '">' + esc(op.method) + "</span>",
      '<span class="api-path">' + esc(op.path) + "</span>",
      '<span class="api-summary">' + esc(op.summary) + "</span>",
      op.deprecated ? '<span class="api-deprecated">已废弃</span>' : "",
      "</summary>",
      '<div class="api-body">',
    ];
    if (op.description) {
      parts.push('<p class="api-desc">' + esc(op.description) + "</p>");
    }
    if (op.params.length) {
      parts.push('<div class="api-label">参数</div>');
      parts.push(tableHtml(["名称", "位置", "必填", "类型", "说明"], op.params.map(function (p) {
        return [esc(p.name), esc(p.in), p.required ? "是" : "否", esc(p.type), esc(p.desc)];
      })));
    }
    if (op.bodyFields.length) {
      parts.push('<div class="api-label">请求体（application/json）</div>');
      parts.push(tableHtml(["字段", "类型", "必填", "说明"], op.bodyFields.map(function (f) {
        return [esc(f.name), esc(f.type), f.required ? "是" : "否", esc(f.desc)];
      })));
    }
    if (op.responses.length) {
      parts.push('<div class="api-label">响应</div>');
      parts.push(tableHtml(["状态码", "说明"], op.responses.map(function (r) {
        return [esc(r.code), esc(r.desc)];
      })));
    }
    parts.push("</div></details>");
    return parts.join("");
  }

  function renderGroups(groups) {
    if (!groups.length) {
      return '<p class="api-empty">没有匹配的接口，换个关键词试试。</p>';
    }
    return groups.map(function (group) {
      return [
        '<section class="api-group">',
        "<h2>" + esc(group.name) + "（" + group.operations.length + "）</h2>",
        group.operations.map(operationHtml).join(""),
        "</section>",
      ].join("\n");
    }).join("\n");
  }

  // ---------------------------------------------------------------- 页面装配
  function countText(groups) {
    var ops = groups.reduce(function (sum, g) { return sum + g.operations.length; }, 0);
    return ops + " 个接口 · " + groups.length + " 组";
  }

  function boot() {
    var body = document.getElementById("api-body");
    var countEl = document.getElementById("api-count");
    var filterEl = document.getElementById("api-filter");
    if (!body) return;

    fetch("/openapi.json")
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (spec) {
        var all = groupEndpoints(spec);
        var paint = function () {
          var shown = filterGroups(all, filterEl ? filterEl.value : "");
          body.innerHTML = renderGroups(shown);
          if (countEl) countEl.textContent = countText(shown);
        };
        if (filterEl) filterEl.addEventListener("input", paint);
        paint();
      })
      .catch(function (err) {
        body.innerHTML = '<p class="api-err">接口文档加载失败：' + esc(err && err.message || err) +
          "（确认后端已启动，且 /openapi.json 可访问）</p>";
        if (countEl) countEl.textContent = "加载失败";
      });
  }

  window.ApiDocs = {
    groupEndpoints: groupEndpoints,
    filterGroups: filterGroups,
    renderGroups: renderGroups,
    typeOf: typeOf,
  };

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", boot);
    } else {
      boot();
    }
  }
})();
