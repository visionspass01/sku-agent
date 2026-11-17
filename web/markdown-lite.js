/* 轻量 Markdown 渲染器（零依赖）：标题/段落/列表/表格/代码块/引用/粗斜体/链接/行内代码。 */
(function () {
  "use strict";

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function inline(text) {
    var codes = [];
    var out = escapeHtml(text);
    out = out.replace(/`([^`]+)`/g, function (_m, code) {
      codes.push(code);
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (_m, label, href) {
      // 文档内锚点（#xxx）立刻就地在当前页滚动，不能像外链那样开新标签
      if (href.charAt(0) === "#") {
        return '<a class="anchor" href="' + href + '">' + label + "</a>";
      }
      return '<a href="' + href + '" target="_blank" rel="noopener">' + label + "</a>";
    });
    out = out.replace(/\u0000(\d+)\u0000/g, function (_m, idx) {
      return "<code>" + codes[Number(idx)] + "</code>";
    });
    return out;
  }

  function splitRow(row) {
    return row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(function (c) {
      return c.trim();
    });
  }

  function isTableRow(line) {
    return typeof line === "string" && line.indexOf("|") !== -1;
  }

  // 标题锚点 id 与 GitHub 一致：小写 → 只保留字母/数字/组合符/下划线/连字符/空格 → 空格转 '-'
  // 同名标题重复出现时追加 -1/-2（GitHub 的去重规则），保证目录链接在两边都能对上
  var slugSeen = Object.create(null);

  function slugify(text) {
    var out = String(text)
      .trim()
      .toLowerCase()
      .replace(/[^\p{L}\p{N}\p{M}\p{Pc}\- ]/gu, "")
      .replace(/ /g, "-");
    return out || "section";
  }

  function uniqueSlug(text) {
    var base = slugify(text);
    var id = base;
    var n = 0;
    while (slugSeen[id]) {
      n += 1;
      id = base + "-" + n;
    }
    slugSeen[id] = true;
    return id;
  }

  function renderMarkdown(source) {
    var lines = String(source).replace(/\r\n/g, "\n").split("\n");
    var html = [];
    var i = 0;
    slugSeen = Object.create(null);   // 每次渲染重置：锚点去重只在同一份文档内生效
    function blank(line) { return !line || !line.trim(); }

    function isBlockStart(line) {
      return /^(#{1,6}\s|```|>|[-*+]\s|\d+\.\s|---+\s*$)/.test(line) ||
        (isTableRow(line));
    }

    while (i < lines.length) {
      var line = lines[i];
      if (blank(line)) { i++; continue; }

      // 代码块
      var fence = line.match(/^```(\w*)\s*$/);
      if (fence) {
        var lang = fence[1];
        i++;
        var buf = [];
        while (i < lines.length && !/^```\s*$/.test(lines[i])) {
          buf.push(lines[i]);
          i++;
        }
        i++;
        html.push("<pre><code" + (lang ? ' class="lang-' + escapeHtml(lang) + '"' : "") + ">" +
          escapeHtml(buf.join("\n")) + "</code></pre>");
        continue;
      }

      // 标题（带锚点 id，供目录/深链跳转）
      var heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        var level = heading[1].length;
        var hid = uniqueSlug(heading[2]);
        html.push('<h' + level + ' id="' + escapeHtml(hid) + '">' +
          inline(heading[2].trim()) + "</h" + level + ">");
        i++;
        continue;
      }

      // 分隔线
      if (/^---+\s*$/.test(line)) { html.push("<hr/>"); i++; continue; }

      // 引用（`>` 里的空行分段：一条引用块内可以有多个段落，目录就靠这个分两行）
      if (/^>\s?/.test(line)) {
        var groups = [];
        var group = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) {
          var qt = lines[i].replace(/^>\s?/, "");
          if (!qt.trim()) {
            if (group.length) { groups.push(group); group = []; }
          } else {
            group.push(qt);
          }
          i++;
        }
        if (group.length) groups.push(group);
        html.push("<blockquote>" + groups.map(function (g) {
          return "<p>" + inline(g.join(" ")) + "</p>";
        }).join("") + "</blockquote>");
        continue;
      }

      // 表格
      if (isTableRow(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        var header = splitRow(line);
        i++;
        void 0; // 跳过对齐行
        i++;
        var rows = [];
        while (i < lines.length && !blank(lines[i]) && isTableRow(lines[i])) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        var thead = "<thead><tr>" + header.map(function (c) {
          return "<th>" + inline(c) + "</th>";
        }).join("") + "</tr></thead>";
        var tbody = "<tbody>" + rows.map(function (cells) {
          return "<tr>" + cells.map(function (c) {
            return "<td>" + inline(c) + "</td>";
          }).join("") + "</tr>";
        }).join("") + "</tbody>";
        html.push("<table>" + thead + tbody + "</table>");
        continue;
      }

      // 无序列表
      if (/^[-*+]\s+/.test(line)) {
        var items = [];
        while (i < lines.length && /^[-*+]\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^[-*+]\s+/, ""));
          i++;
        }
        html.push("<ul>" + items.map(function (t) {
          return "<li>" + inline(t) + "</li>";
        }).join("") + "</ul>");
        continue;
      }

      // 有序列表
      if (/^\d+\.\s+/.test(line)) {
        var oItems = [];
        while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
          oItems.push(lines[i].replace(/^\d+\.\s+/, ""));
          i++;
        }
        html.push("<ol>" + oItems.map(function (t) {
          return "<li>" + inline(t) + "</li>";
        }).join("") + "</ol>");
        continue;
      }

      // 段落
      var para = [];
      while (i < lines.length && !blank(lines[i]) && !isBlockStart(lines[i])) {
        para.push(lines[i].trim());
        i++;
      }
      html.push("<p>" + inline(para.join(" ")) + "</p>");
    }
    return html.join("\n");
  }

  window.renderMarkdown = renderMarkdown;
})();
