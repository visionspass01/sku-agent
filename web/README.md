# Web 前端（原生 JavaScript，无构建）

> 本目录存放 Agent 演示所需全部前端资源。不依赖 Node.js 与打包工具，浏览器直接加载，由 Agent 服务的 `GET /`、`GET /agents.html` 路由注入资源版本号后返回。

## 目录结构

```
web/
├── index.html          # SKU 监管 Agent 演示页（入口 /，服务端渲染注入 ?v=）
├── app.js             # 演示页逻辑：SSE 流式渲染、运行详情、记忆面板、历史会话、指标卡（2897 行）
├── agents.html        # Agent 目录页（/agents.html，一页一个 Agent 的入口；扩展说明只在页首注释里）
├── agents.js          # 目录页逻辑：AGENT_PAGES 数据驱动渲染 + /api/health 状态（147 行）
├── api.html           # 接口文档页（/api.html，同源读 /openapi.json，不依赖 CDN）
├── api.js             # 接口文档渲染：分组 / 折叠 / 过滤（纯函数挂在 window.ApiDocs 上便于单测）
├── about.html         # 项目简介渲染页（fetch /about.md 后本地渲染）
├── markdown-lite.js    # 轻量 Markdown 渲染（about.html 使用；标题自动生成 GitHub 同款锚点 id，文首目录可页内跳转）
└── style.css           # 全站样式（暗色主题，CSS 变量 + 面板/表格/卡片/弹窗，646 行）
```

## 本地启动

前端无独立开发服务器，随 Agent 服务一起启动：

```bash
PYTHONPATH=src uvicorn app.main:app --port 8000
```

打开：

- <http://127.0.0.1:8000/> — SKU 监管 Agent 演示页
- <http://127.0.0.1:8000/agents.html> — Agent 目录页
- <http://127.0.0.1:8000/about.html> — 项目简介

演示账号：`admin / hhw123`（管理员）、`worker1 / hhw111`（男装）、`worker2 / hhw222`（女装）。

## 页面与交互

| 区域 | 说明 | 依赖接口 |
|------|------|----------|
| 顶部徽章 | 存储 / 缓存 / 模型 / MCP 运行形态 | `GET /api/health` |
| 指标卡 | SKU 总数、异常 SKU、缓存命中率（含 P50/P95）、LLM 调用与 Token、队列；点击展开明细 | `GET /api/metrics`、`GET /api/cache/breakdown` |
| 单 SKU 研判 | 输入 SKU 触发研判，展示风险结论、证据、预案、校验清单与执行轨迹 | `POST /api/inspect`、`GET /api/inspections/{run_id}` |
| 异常列表 | 量化筛查结果，点击行直接发起研判 | `GET /api/anomalies` |
| 批量巡检 | 模式与数量选择、实时进度、协作式终止、简报与最近 10 次日志 | `POST /api/patrol`、`GET /api/patrol/{run_id}` |
| SOP 检索 | 知识与口径检索，展示 `chunk_id`、冲突提示与范围说明 | `GET /api/rag/search` |
| AI 助手 | SSE 流式回答；可展开运行详情、事实卡、工作集实体表；支持历史会话回放 | `POST /api/chat/stream`、`GET /api/chat/{session_id}`、`GET /api/memory/sessions/{id}` |
| 人工确认 | 补货草稿单与售后工单的确认/驳回，含详情弹层 | `GET /api/actions/pending`、`POST /api/actions/{id}/approve` |
| 账号 | 登录、切换账号、安全记录、登出 | `/api/auth/*` |
| 项目简介 | `/about.html` 渲染 `docs/ABOUT.md`（两大部分：项目结构 / 技术设计）；文首「目录」的锚点在页内平滑跳转，页内链接不开新标签页 | `GET /about.md` |

流式渲染细节：`app.js` 先建立 SSE 连接，按事件类型分别处理 `stage` / `llm_start` / `llm_end` / `tool_start` / `tool_end` / `delta` / `tick` / `done`；收到 `delta` 后逐字追加到气泡，`done` 时用最终答复覆盖（溯源重写可能改变内容）；收到 `fatal=true` 的错误事件不再降级重发。

## 资源版本与自检

- `src/app/static_assets.py` 在服务端为 `app.js` / `agents.js` / `style.css` 注入内容哈希（`?v=`），页面与脚本版本天然一致。
- 页面内置兜底脚本：若脚本未加载或与页面版本不一致，顶部显示黄色横幅并提供「强制刷新」。
- 目录页与演示页各自带 build tag（`data-ui-build`），不一致时同样提示。

## 扩展：新增一个 Agent 演示页

1. 新增 `web/<page>.html`，复用 `style.css` 的头部、面板与卡片样式；
2. 在 `web/agents.js` 的 `AGENT_PAGES` 数组追加一条数据（`id` / `name` / `page` / `tagline` / `caps` / `accounts`）；
3. 如需服务端注入 `?v=` 版本号，在 `src/app/main.py` 增加一条 `render_page_html("<page>.html")` 路由；
4. 运行 `node scripts/ui_smoke.js` 确认前端冒烟通过。

`tests/test_ui_pages.py` 会校验「每条 `page` 都指向真实存在的 HTML 且 `id` 不重复」，遗漏会被 CI 拦住。

## 前端冒烟测试

```bash
node scripts/ui_smoke.js      # 无浏览器：最小 DOM 垫片加载 app.js，跑一遍关键交互
```

覆盖场景包括：流式对话渲染、运行详情七段结构、事实卡与工作集、历史会话回放、进度卡阶段与耗时、缓存/延迟明细弹窗、计时卡片、功能介绍随勾选变化、版本横幅提示等；对应 pytest 入口为 `tests/test_observability.py::test_ui_smoke_script_passes`。
