# 服装电商 SKU 库存监管 Agent — LangGraph 多 Agent + MCP + RAG

> 基于 LangGraph + MCP + RAG 的服装电商 SKU 库存监管智能体，覆盖自然语言问数、SKU 研判、补货预案与人工审批、批量异常巡检与 SOP 溯源，全部业务口径以代码与测试固化。

---

## 项目简介

本项目是一个面向服装电商「款式 × 颜色 × 尺码」多维库存监管场景的 **AI Agent 系统**。系统由大语言模型（DeepSeek）驱动，通过 MCP 协议与数据中台交互，实现：

- 自然语言问数（库存、销量、售罄天数、断码、补货量），每个数值带 `result_id` 与 `as_of`
- 单 SKU 研判（风险结论、数据证据、处置预案、校验清单、执行轨迹）
- 补货预案生成（安全库存、再订货点、建议补货量），超阈值仅建草稿单并转人工确认
- 批量异常巡检（量化筛查 → 队列幂等入队 → 并发出队 → 汇总简报）
- SOP 知识检索与引用溯源（`chunk_id`），检索不到时明确弃答
- 跨部门口径冲突裁决（权威度排序 + 部门可比性闸门）
- 部门权限隔离与写操作人工确认（HITL），审批与售后为单条原子操作
- 全链路可观测（每轮运行详情、延迟分位、工具可靠性、trace_id 贯通）

工程侧同时提供 1500+ 条离线测试、12 道 CI 门禁、11 个评测脚本与 4 个基准脚本，所有对外数字均可一条命令复现。

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| **LLM** | DeepSeek（LangChain `ChatOpenAI`） | 模型与单价可配置，成本按 Token 估算 |
| **Agent 框架** | LangGraph 1.2 + LangChain Tools | 状态图编排、子图、Command、checkpointer |
| **MCP 协议** | FastMCP 1.30 | 三传输（memory / stdio / direct）+ 连接池 + 熔断 |
| **Web 框架** | FastAPI + Uvicorn | REST + SSE 流式响应 |
| **前端** | 原生 JavaScript | 无构建步骤，浏览器直接加载 |
| **数据库** | SQLite / MySQL 8.0 | 本地零依赖启动，容器部署使用 MySQL |
| **缓存与队列** | Redis 7 / fakeredis | TTL 抖动缓存、巡检 Stream 消费组、分布式锁 |
| **检索** | BM25 + 512 维哈希向量 + RRF + 可插拔重排 | 可选腾讯云 TokenHub Embedding + Qdrant |
| **测试与质量** | pytest + coverage、ruff、Node DOM 冒烟、GitHub Actions | 1500+ 条离线用例，覆盖率 89% |
| **部署** | Docker Compose | 六服务编排，4C4G 单机可运行 |

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────────────┐
│               Web 界面（原生 JS，无构建）:8000                         │
│   Agent 目录页 /agents.html · 演示页 / · 项目简介 /about.html          │
│   指标卡 · 单 SKU 研判 · 异常列表 · 批量巡检 · SOP 检索 · AI 助手       │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ HTTP / SSE
┌───────────────────────────────▼──────────────────────────────────────┐
│                      Agent 服务（FastAPI）:8000                       │
│  对话链路：ReAct 工具循环 → SSE 流式输出 → 出口溯源闸门                 │
│  研判链路：Supervisor → collector → Skill 子图 → Reasoner → Verifier   │
│  记忆：L0 工具契约 · L1 会话（事实卡/工作集）· L2 工作区 · L3 组织档案   │
│  治理：部门作用域守卫 · 工具循环闸门 · 限流 · 降级阶梯 · 可观测         │
└───────────────┬──────────────────────────────┬───────────────────────┘
                │ MCP（memory / stdio / direct）│ HTTP REST
┌───────────────▼──────────────────┐  ┌────────▼───────────────────────┐
│       MCP Server（FastMCP）       │  │      数据中台（FastAPI）:8001    │
│  erp 7 · pailitao 3 · order 5    │  │  mock 源 → Airflow 模拟         │
│  = 15 个工具，仅做端点映射        │  │  → dbt 式分层转换 → 治理口径     │
│  连接池 2 会话 · 熔断 3 次/60s    │  │  → REST + TTL 抖动缓存 + 审计    │
└──────────────────────────────────┘  └────────┬───────────────────────┘
                                               │
┌──────────────────────────────────────────────▼───────────────────────┐
│  存储：SQLite / MySQL（业务）· Redis（缓存/队列/锁）· checkpointer   │
│  检索（可选）：Qdrant 向量库 · 腾讯 Embedding，不可用则降级内存向量  │
└──────────────────────────────────────────────────────────────────────┘
```

分层原则：**中台是边界，Agent 是纵深**。中台按调用方身份做授权、配额与审计，Agent 在工具层做部门作用域二次校验；MCP 层不直连数据库，只承担「工具名 → 中台端点」映射，因此中台可独立部署、独立替换。

---

## 核心功能

### 1. 多 Agent 研判与校验回流

`Supervisor` 决定采集子集（ERP 必选、竞品可选）→ `collector` 并发采集（未派发的 source 不发起任何 MCP 调用）→ 库存研判与补货测算两个 Skill 子图并行 → `Reasoner` 输出结构化预案 → `Verifier` 执行确定性校验（引用有效性、数字一致性、折扣合规、补货合规、SOP 顺序）并交由模型复核 → 不通过则携带问题回 `Reasoner`，最多 2 轮，超限显式标记 `degraded`。

评测：`eval_apparel.py` 12 项独立复算、`eval_degradation.py` 8 个降级场景；测试：`tests/test_graph_routing.py`（`tasks=["erp"]` 时竞品调用数为 0）。

### 2. 对话链路：ReAct + SSE 流式 + 出口溯源闸门

```
用户提问 → 记忆注入（L2 基线 / L1 事实卡 / 工作集）→ query_plan
        → ReAct 工具循环（27 个工具）→ SSE 推送 delta 增量
        → 出口溯源闸门（来源 id 校验 / 数字溯源）
        → 通过输出 · 不通过重写一次 · 仍不达标标 degraded
```

- **SSE 流式输出**：`astream(stream_mode=["messages","values"])` 逐 Token 推送 `delta`，过滤工具调用片段与工具消息，工具返回值不会进入答案正文。
- **出口溯源闸门**：答案中每个 `res_xxx` 必须来自本轮工具结果；带单位数字未标来源判为失败，模型重写一次后仍不达标则标记 `degraded` 并在界面展示告警条。
- **状态码语义**：会话不存在 404、同会话并发 409、频次超限 429、锁后端不可用 503，均在建立流之前返回，前端不对这类错误降级重发。

### 3. 工具层与 MCP

3 个 FastMCP Server（ERP 7 / 拍立淘 3 / 订单售后 5）对中台接口做标准化封装，对话侧另有 27 个工具（读取、写、订单、风格、记忆、技能、审批、知识库、元数据共 9 个分组）。同一 Server 默认持有 2 条会话，调用投递给最空闲的一条；连续 3 次失败熔断 60 秒，窗口内快速失败。工具分组与作用域声明以中台能力目录为唯一来源，Agent 侧使用离线快照启动，`/api/health.capabilities` 标注目录漂移。

基准：`bench_mcp.py` 6 个并发取数，pool=1 约 564ms → pool=2 约 278ms（约 2.0 倍，数值随机器变化）。

### 4. 四层记忆与上下文工程

| 层级 | 内容 | 作用范围 |
|------|------|----------|
| L0 工具契约 | `result_id` / `as_of` / `summary` / `truncated` / `artifact_path` | 单次调用 |
| L1 会话记忆 | 事实卡（结论骨架）、工作集（实体表）、用户偏好 | 单会话 |
| L2 工作区 | `AGENTS.md` 基线 + Skills 按需加载 | 跨会话稳定 |
| L3 组织记忆 | 分析档案，支持跨会话检索与引用 | 跨会话 |

单次工具调用内联明细上限 50 行，超出时只回前 3 行、`row_count` 与 `artifact_path`，完整明细落 `data/artifacts/<session>/<result_id>.jsonl` 供人工与审计查看；Agent 侧不提供回读工具，避免明细占满上下文。提示词外置为 Markdown 并按内容哈希生成版本号，每轮记录上下文缓存命中情况。

### 5. RAG 检索与口径冲突裁决

```
清洗 → 分块（doc#哈希，320 字滑窗/60 重叠）→ 字段加权 BM25 + 哈希向量双路召回
     → RRF 融合（k=60）→ 可插拔重排（none / heuristic / llm / cross）
     → 同文档配额 + MMR → chunk_id 输出
```

- **无答案优先于凑答案**：判据由相似度、覆盖率与词法证据构成，20 条负样本标定，正确弃答率 90%。
- **冲突按权威度裁决**：先确定问题所属部门（可比性闸门），仅与「该部门 + 通用」口径比较；可比集合内按 中台治理口径 > 部门细则 > 通用规则 > 历史案例 排序，同权威度再比版本与生效日。落败文档保留并标 `is_primary`，胜出片段原文随 `## 冲突提示` 一并给模型。
- **状态可区分**：无相关可比口径报 `unavailable`，仅单一来源报 `none` 并附 `single_source_keys`，不宣称「无冲突」。

评测：38 题回归集 R@1 100%；消融 BM25 92.1% / 向量 94.7% / RRF 100%；口径冲突 25/25；1200 篇规模基准 R@1 97.4%、P95 69.4ms。

### 6. 人工协同（HITL）与读写分离

补货（≥200 件或 ≥5 万元）与售后均只创建草稿单，由操作人在「人工确认」面板确认或驳回，并记录审批账号与时间；写操作携带幂等键，重试不会重复建单；Agent 不持有任何审批工具；写工具按角色白名单开放（默认仅 `admin`），部门账号调用返回「无权限执行写操作」。

### 7. 服装行业口径（按码看，不按款平均）

SKU 级「库存充足」常掩盖断码压货：主销码售罄、其余尺码积压。因此中台在 SKU 维度之外增加款级 mart：尺码曲线（各码销量占比，≥15% 为主销码）、按码补缺口的补货建议、齐码率与断码率（断码只计主销码缺货）、周转天数（30 天口径）、售罄率与生命周期阶段（上新 / 主推 / 清仓 / 长尾，清仓与补货互斥）。

批量研判同样使用可用库存口径（可用 = 现存 − 锁定 − CRM 预订 − 订单占用），断码仅认「本码为主销码且可用为 0」，旧归档无主销码标记时回落旧判据并显式标 `legacy`。

### 8. 可观测性与成本

SSE 实时进度（阶段 / 模型调用 / 工具 / tick）→ 每轮 `run_detail`（按发生顺序的时间轴、耗时构成、Token 成本、权限命中、溯源自检）→ 进程指标（按类别延迟 P50/P95、工具成功率与失败分类、缓存命中率、队列状态）。一轮对话共用一个 `trace_id`，贯通中台审计、MCP 调用、RAG 检索、模型回调与轮次日志；`/api/metrics/agents` 提供按 Agent 的轮次、延迟、Token、成本与降级率聚合。

### 9. Agent 注册表与热插拔

对话侧提供 3 个 Agent：`analyst`（通用问数，全量工具）、`diagnostician`（库存诊断，只读）、`operator`（建单 / 售后 / 物流，写操作走人工确认）。由声明式 manifest 定义（`registry/*.json` + `prompts/*.md`），按「显式指定 > 关键词 > `query_plan.intent` > 默认」确定性路由，路由准确率 100%（26/26）。新增 Agent 只需增加一个 JSON 与一个 Markdown 文件，注册表按目录与文件 mtime 热加载，无需改代码或重启；中台侧支持运行时注册调用方（`POST /api/v1/agents`），令牌仅返回一次并以 sha256 存储。

### 10. 数据中台治理

一个 Key 对应一个 `agent_id`，scope 分为 `read / write / approve / etl / admin`，按调用方限流（429 + `Retry-After`），每次调用写 `api_audit_log`；`GET /api/v1/metadata/capabilities` 是工具分组与作用域声明的唯一来源；所有响应带 `schema_version` 与 `X-Platform-Contract` 响应头，字段漂移在 CI 阶段即可发现；审批与售后采用单条条件更新，并发确认仅一个成功，另一个返回 409。

### 11. 降级阶梯与循环闸门

| 依赖 | 不可用时 |
|------|----------|
| Redis | 退化为 fakeredis（进程内缓存与队列） |
| Qdrant | 退化为内存向量检索 |
| Embedding | 退化为哈希向量 |
| 数据中台 | 工具返回结构化错误并进入熔断，不阻塞对话 |
| 模型 | 瞬时失败重试，额度耗尽切备用模型；已推送增量后不重试 |

ReAct 循环由模型决定何时停止，框架默认 `recursion_limit` 在 langgraph 1.2.19 为 10007 个超步；本项目在工具层按「连续失败 3 次 / 同工具同入参指纹连续成功 3 次 / 超出单轮预算」立即中止，图层再以 `3 × 预算 + 6` 兜底。中止时写入 `loop_aborted` 步骤、工具统计、SSE 事件与错误日志，并以 422 + `error_code` / `trace_id` 返回（流式响应带 `fatal=true`，前端不再自动重发）。

---

## 项目结构

```
sku-agent-demo/
├── src/app/                            # Agent 服务
│   ├── agents/                         # LangGraph 主图、注册表、提示词、checkpointer、grounding、handoff
│   │   ├── registry/                   # 声明式 Agent manifest（analyst / diagnostician / operator）
│   │   ├── prompts/                    # 6 份提示词（对话 / 主管 / 研判 / 校验 / 诊断 / 操作）
│   │   ├── skills/                     # 5 份技能手册（断码诊断 / 补货测算 / 清仓复核 / 初筛 / 周度回顾）
│   │   ├── graph.py                    # 研判主图（Supervisor → collector → merge → refine → reasoner → verifier → finalize）
│   │   ├── chat_agent.py               # 对话链路（ReAct + SSE 流式 + 运行详情）
│   │   ├── tool_guard.py               # 工具循环闸门（失败 / 重复 / 预算三层判定）
│   │   └── grounding.py                # 出口溯源闸门
│   ├── tools/                          # 对话工具（27 个，9 个分组）+ 中台客户端
│   ├── mcp_servers/                    # FastMCP：erp(7) / pailitao(3) / order(5)，仅做端点映射
│   ├── mcp_client/                     # 三传输 + 连接池 + 熔断 + 缓存
│   ├── rag/                            # 清洗 / 分块 / 索引 / 混合召回 / 重排 / embedding / vector_store
│   ├── memory/                         # L0 工具契约 / L1 会话记忆 / L2 工作区 / L3 组织档案
│   ├── observability/                  # 延迟分位 / 工具可靠性 / LLM 回调 / OTel / 运行时状态
│   ├── security/                       # 限流底座 + 部门作用域守卫
│   ├── api/                            # 路由（对话 / 巡检 / 审批 / 记忆）+ SSE
│   └── pipeline/                       # 量化筛查 / 小时快照 / 指标 / 巡检执行器
│
├── src/data_platform/                  # 轻量数据中台
│   ├── api/                            # REST 路由 + 身份/scope/配额/审计
│   ├── ingest/                         # mock 源 → Airflow 模拟 → PII 脱敏
│   ├── dbt_like/                       # stg → dim/fct → mart 分层转换
│   └── governance.py                   # 指标口径注册 + 脱敏规则 + 审计
│
├── web/                                # 前端页面（原生 JS，无构建）
│   ├── index.html                      # SKU 监管 Agent 演示页
│   ├── agents.html / agents.js         # Agent 目录页（一页一 Agent）
│   └── about.html                      # 项目简介（渲染 docs/ABOUT.md）
│
├── docs/                               # 8 份工程文档（架构 / API / 配置 / 部署 / RAG / UI / 简介 / 一页纸）
├── PYTHON_BACKEND_GUIDE.md             # 逐模块代码导读（11 章）
├── scripts/                            # 初始化 / 评测 / 基准 / 校验 / 演示脚本
├── tests/                              # 1500+ 条离线测试（收集数随用例增长，以 CI 为准）
├── docker/                             # Dockerfile、compose 编排、nginx、初始化 SQL
├── AGENTS.md                           # L2 工作区基线（运行时注入系统提示）
├── data/kb/                            # 14 篇知识库（67 个片段：通用 9 篇 SOP + 男装 2 + 女装 2 + 历史案例 1）
└── data/eval/                          # 评测题集与基准结果
```

---

## 快速启动

### 环境要求

- Python 3.11
- 无需数据库、Redis、向量库即可运行（直接 uvicorn 启动时：默认 SQLite + fakeredis + 内存向量）
- 容器部署另需 Docker 与 Docker Compose，建议 4 核 4G 以上
- DeepSeek API Key（可选；留空时对话降级，测试与界面仍可运行）

三种启动形态的外部依赖对照（**部署态 ≠ 上面的默认态**，这是刻意设计：默认值面向零依赖本地运行，compose 用环境变量切到生产同构栈）：

| 形态 | 关系库 | 缓存 | 向量 | Embedding |
|---|---|---|---|---|
| **裸跑**（两条 `uvicorn`） | SQLite（`data/sku_demo.db`） | fakeredis（进程内） | 内存（numpy 余弦） | 哈希向量 512 维 |
| **dev compose**（`docker/docker-compose.yml`） | MySQL 8.0 | Redis 7（`REDIS_URL` 硬编码） | 内存 | 哈希向量 512 维 |
| **deploy compose**（`docker/docker-compose.deploy.yml`） | MySQL 8.0 | Redis（可选，`REDIS_URL` 默认空 → 容器内 fakeredis；`--profile redis` 启用） | Qdrant 1.12 | 腾讯云 TokenHub（未配 Key 时降级哈希向量） |

三者都带「外部组件不可用即降级」的兜底：Redis 不可用退化进程内缓存、Qdrant 不可用退化内存检索、Embedding 不可用退化哈希向量，降级原因写入 `/api/health`。

### 1. 安装依赖

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

```bash
# 大模型（留空也能启动，仅问答会降级）
DEEPSEEK_API_KEY=sk-your-api-key
LLM_MODEL=deepseek-flash

# 关系库：本地默认 SQLite；容器部署默认 MySQL
# DATABASE_URL=sqlite:///data/sku_demo.db

# 缓存与队列：留空使用 fakeredis
# REDIS_URL=redis://127.0.0.1:6379/0

# 检索后端：本地默认内存 + 哈希向量；上量后切 qdrant / tencent
VECTOR_STORE=memory
EMBEDDING_BACKEND=hash

# 数据中台地址
PLATFORM_API_URL=http://127.0.0.1:8001
```

### 3. 初始化演示数据

```bash
PYTHONPATH=src python scripts/init_db.py            # 300 SKU × 30 天流水 + 订单样例 + 知识库索引
PYTHONPATH=src python scripts/platform_bootstrap.py # 中台：模拟源 → 入湖 → dbt 式转换
```

### 4. 启动数据中台（端口 8001）

```bash
PYTHONPATH=src uvicorn data_platform.api.main:app --port 8001
```

### 5. 启动 Agent 服务（端口 8000）

```bash
PYTHONPATH=src uvicorn app.main:app --port 8000
```

启动日志中可确认数据库、缓存、检索与 MCP 状态；中台不可用时仅告警并记录降级原因，不阻塞启动。

### 6. 访问应用

| 入口 | 地址 | 说明 |
|------|------|------|
| SKU 监管 Agent 演示页 | <http://127.0.0.1:8000/> | 指标卡、单 SKU 研判、批量巡检、SOP 检索、AI 助手、人工确认 |
| Agent 目录页 | <http://127.0.0.1:8000/agents.html> | 一页一个 Agent 的入口与运行状态 |
| 项目简介 | <http://127.0.0.1:8000/about.html> | 长文自述，与 `docs/ABOUT.md` 同源 |
| 接口文档 | <http://127.0.0.1:8000/docs> | OpenAPI 交互式文档 |

演示账号：`admin / hhw123`（管理员）、`worker1 / hhw111`（男装部门）、`worker2 / hhw222`（女装部门）。

### 7. 容器部署

```bash
cp .env.example .env
# 生成 443 兜底证书（浏览器默认用 https 打开 IP 时 301 到 http；缺证书 nginx 起不来）
bash scripts/gen_redirect_cert.sh
docker compose --env-file .env -f docker/docker-compose.deploy.yml up -d --build
```

内存预算、TLS（含正式证书覆盖方式）、TokenHub 接入与排障说明见 [docs/DEPLOY.md](docs/DEPLOY.md)。

---

## 启动顺序总结

```
数据中台(:8001) → Agent 服务(:8000) → 浏览器访问 → 可选：Docker 六服务编排
```

> 说明：Agent 启动时会同步中台能力目录与 SKU 参考目录，因此建议先启动数据中台；目录同步失败仅记录告警，服务仍可启动，但工具调用会返回结构化错误。

---

## 项目亮点

### 1. 决策可追溯，而非仅生成文本

答案中的每个数字都绑定 `result_id` 与 `as_of`，出口溯源闸门在返回前校验来源真实性（来源 id 必须来自本轮工具结果）；不通过先重写一次，仍不通过则显式降级并在界面提示，不静默放行。

### 2. 研判链路有反馈闭环

研判不是单次生成：Verifier 用确定性规则校验引用、数字、折扣、补货合规与 SOP 顺序，问题回传 Reasoner 重写，最多 2 轮；超限显式标记 `degraded`，`run_detail` 中可查每轮校验结论。

### 3. 四层记忆与上下文压缩

L0 工具契约 → L1 会话事实卡与工作集 → L2 工作区基线 → L3 组织档案。单次明细超过 50 行落 artifact，提示词按「静态在前、动态在后」组织以获得前缀缓存命中，记忆与成本都有对应字段可查。

### 4. 检索不止召回，还要裁决冲突

同一口径多份规定时按权威度取一条并说明理由；先确定部门面（可比性闸门）再比较，跨部门比较不做单一裁决；无相关口径报 `unavailable`、单一来源报 `none`，不宣称「无冲突」。

### 5. 读多写少场景下的权限与审批设计

写操作只建草稿单，人工确认后流转；写工具按角色白名单开放；审批与售后使用单条条件更新，并发确认仅一个成功；部门账号越权访问返回「无权限」，不泄露数据存在性。

### 6. 双保险的循环闸门

框架默认 `recursion_limit` 近似无上限，因此工具层按失败、重复与预算三层判定立即中止，图层再以 `3 × 预算 + 6` 兜底；中止轮次留下结构化证据（`loop_aborted` 步骤、工具统计、SSE 事件、错误码 + trace_id）。

### 7. 降级路径可演练

Redis、Qdrant、Embedding、数据中台、模型五类依赖均有降级阶梯，`eval_degradation.py` 用 6 类故障 × 8 个断言场景验证「可诊断 + 快速失败 + 不静默降质」。

### 8. 口径以代码与测试固化

安全库存、再订货点、主销码、断码、清仓与补货阈值既写在 `AGENTS.md`（运行时注入），也在 Skill 与评测中复算：`eval_apparel.py` 12 项、`eval_agent_tooling.py` 第 6 组校验 skills 文案与代码口径一致。

### 9. 面向多 Agent 演进的页面与注册表

前端按「一页一个 Agent」组织，目录页由数据驱动；后端 Agent 由 manifest 声明并可热加载。新增演示页或 Agent 均不改动既有代码结构。

### 10. 对外数字可复现

1500+ 条离线测试、12 道 CI 门禁、11 个评测脚本与 4 个基准脚本；检索、权限、治理、口径、降级、并发等指标均提供单条命令与固定题集，文档中的数值标注为实测口径。

---

## 评测结果

| 指标 | 结果 | 命令 |
|------|------|------|
| 离线测试 | 1500+ 收集全绿，覆盖率 89% | `pytest --cov=src/app --cov=src/data_platform` |
| Agent 路由 | 100%（26/26） | `scripts/eval_routing.py --min-accuracy 0.9` |
| 工具部门作用域 | 100%（34/34） | `scripts/eval_permissions.py --min-pass 1.0` |
| 中台治理 | 100%（30/30） | `scripts/eval_platform_governance.py` |
| Agent 工具面 / 写路径 | 100%（25/25） | `scripts/eval_agent_tooling.py` |
| 出口溯源闸门 | 准确率 100%、误杀率 0% | `scripts/eval_grounding.py --max-false-positive 0.1` |
| 降级路径 | 8/8 场景通过 | `scripts/eval_degradation.py` |
| 服装口径 | 12/12 独立复算通过 | `scripts/eval_apparel.py` |
| 检索回归（38 题） | R@1 100%、R@5 100%、MRR 1.000；负样本弃答率 90% | `scripts/eval_rag.py --negatives` |
| 检索消融 | BM25 92.1% / 向量 94.7% / RRF 100%（R@1） | `scripts/eval_rag.py --ablation` |
| 口径冲突裁决 | 25/25 | `scripts/eval_rag.py --conflicts` |
| 检索规模（1200 篇） | 索引 29.1s、P50 45.9ms / P95 69.4ms、R@1 97.4%、R@5 100% | `scripts/bench_rag_scale.py --docs 1200` |
| MCP 连接池 | pool=1 约 564ms → pool=2 约 278ms（约 2.0 倍） | `scripts/bench_mcp.py` |
| 对话并发 | 闸门 2：5.2s / 4.7 rps；闸门 8：2.7s / 9.0 rps | `scripts/bench_chat.py --mock` |
| 数据规模 | 300 SKU / 69,241 条流水 / 14 篇知识库（67 片段）/ 120 订单 / 100 物流 / 15 售后 | `scripts/verify_stack.py` |

> 并发、延迟与规模类数字与机器负载相关；模型输出存在采样波动，评测脚本打印的均为每次实际运行值。

---

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查与各后端状态（数据库 / 缓存 / 检索 / MCP / 限流） |
| GET | `/api/metrics` | 指标聚合（SKU、异常、缓存、LLM、队列、工具可靠性、延迟分位） |
| GET | `/api/metrics/agents` | 按 Agent 聚合轮次、延迟、Token、成本与降级率 |
| POST | `/api/chat` | 非流式对话 |
| POST | `/api/chat/stream` | SSE 流式对话 |
| GET | `/api/chat/sessions` | 最近会话列表 |
| GET | `/api/chat/{session_id}` | 会话回放（含运行详情与事实卡快照） |
| POST | `/api/inspect` | 单 SKU 研判 |
| GET | `/api/inspections/{run_id}` | 研判结果 |
| POST | `/api/inspect/followup` | 分析线程追问（复用上一轮采集） |
| POST | `/api/patrol` | 触发批量巡检 |
| GET | `/api/patrol/{run_id}` | 巡检进度 |
| POST | `/api/patrol/{run_id}/cancel` | 协作式终止巡检 |
| GET | `/api/rag/search` | SOP 检索（含口径冲突诊断） |
| GET | `/api/skus` / `/api/anomalies` | SKU 定位 / 异常列表 |
| GET | `/api/actions/pending` | 待人工确认的补货草稿单 |
| POST | `/api/actions/{id}/approve` / `reject` | 审批或驳回 |
| POST | `/api/auth/login` / `logout` | 登录与登出 |

完整字段、错误码（含 `X-Error-Code` / `X-Trace-Id` / SSE `fatal`）与工具作用域矩阵见 [docs/API.md](docs/API.md)。

---

## 开发说明

- 一页纸速览（定位 / 关键数字 / 能力边界）：[docs/ONE_PAGER.md](docs/ONE_PAGER.md)
- 业务背景、数据口径与设计取舍长文（看板 `/about.html` 同源）：[docs/ABOUT.md](docs/ABOUT.md)
- **逐模块代码导读**：[PYTHON_BACKEND_GUIDE.md](PYTHON_BACKEND_GUIDE.md)（11 章 + 3 个附录，按逻辑顺序解析 `src/app/` 与 `src/data_platform/` 的每个模块、关键函数与对应测试）
- 分层数据流、图编排、检索栈、记忆与关键取舍：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 接口契约与响应字段：[docs/API.md](docs/API.md)
- 全部环境变量与默认值：[docs/CONFIG.md](docs/CONFIG.md)
- 检索链路与评测方法：[docs/RAG.md](docs/RAG.md)
- 页面与面板使用说明：[docs/UI_GUIDE.md](docs/UI_GUIDE.md)
- 服务器部署与排障：[docs/DEPLOY.md](docs/DEPLOY.md)
- 修改 Agent 行为：编辑 `src/app/agents/prompts/*.md`（内容哈希即时生效，版本号见 `/api/health.prompts`）
- 新增工具：在 `src/app/tools/platform_tools.py` 实现并在能力目录登记，缺少参数说明或作用域声明会在装配期报错
- 新增 Agent：增加 `src/app/agents/registry/<id>.json` 与对应提示词，注册表热加载生效
- 新增技能：在 `src/app/agents/skills/` 增加 Markdown，模型按需 `load_skill` 加载
- 新增 Agent 演示页：新增 `web/<page>.html` 并在 `web/agents.js` 的 `AGENT_PAGES` 增加一条数据（详见 [web/README.md](web/README.md)）

---

## 合规与许可

- 业务数据（SKU、库存、销量、客户、订单、物流）均为程序生成的模拟数据，不含真实用户信息；
- 客户字段在入湖前已完成脱敏（如 `138****1234`、`张*`）；
- 未接入任何第三方生产系统；DeepSeek 与腾讯云 TokenHub 仅在配置自有 Key 时调用，仓库不含任何密钥。

本项目基于 [MIT](./LICENSE) 许可协议开源。


---

## 第二个 Agent：电商生图 Agent（`/image.html`）

同一仓库里的第二个演示 Agent（与 SKU 监管 Agent 并行、互不影响）：**服装电商图生图**。

- 入口：`/agents.html` → 「电商生图 Agent」卡片 → `/image.html`（独立登录态）
- 输入：用户在**中台商品清单里批量选中商品（1–5 个）** + 自然语言需求；参考图有三种来源（任务级互斥）：**中台商品图**（默认，颜色锁定）/ **人工上传素材库** / **拍立淘相似款**（销量 >100 的前 5 个商品主图，搜不到直接报错）
- 参数：构图 / 场景风格 / 9 图位策略 / 张数默认 / 平台规则 / 商品数据 **全部来自数据中台（经 MCP）**，中台不可用直接 503 让人重试（不静默降级）
- 执行：9 图位规划 → 五槽位 Prompt（七条铁律）→ RAG 写法片段 → 本地生图插件（`IMAGE_PROVIDER=fake` 出占位图；真实 Seedream 后置）→ Critic 评审 → Reflexion 重写问题图位
- 产出：图片存本地 24h（或 COS，后置）；**结果元数据（不含图片）回写中台**，失败进 outbox 可重放
- 台账：独立账号（`admin / hhw123`、`worker1 / hhw111`、`worker2 / hhw222`）、配额（按**使用量**占额度）、管理员面板（SVG 折线图 + 5 张卡：使用量·发起量·成功率·上架率·配额·在途成本）

### 快速体验（全部离线，无需 Key）

```bash
# 1) 数据中台（image 模块：商品/预设/结果表）
PYTHONPATH=src uvicorn data_platform.api.main:app --port 8001

# 2) Agent（同时挂 SKU 与生图两个 Agent）
PYTHONPATH=src uvicorn app.main:app --port 8000
# 浏览器打开 http://127.0.0.1:8000/image.html ，用 admin / hhw123 登录
```

`.env` 里把 `IMAGE_PRODUCT_SOURCE=fake`（默认，本地替身）改成 `mcp` 即走真实中台接口。

### 接口一览

| 分类 | 接口 |
|---|---|
| 账号 | `POST /api/image/auth/login` · `GET /me` · `POST /logout` · `POST /verify` |
| 参数 | `GET /api/image/products` · `GET /api/image/catalog`（均转发中台） |
| 任务 | `POST /api/image/tasks`（批量选商品）· `GET /tasks` · `GET /tasks/{id}` · `GET /tasks/{id}/stream`(SSE) · `POST /tasks/{id}/cancel` · `POST /tasks/{id}/retry` |
| 批次 | `POST /api/image/tasks/cancel-batch` · `POST /api/image/tasks/retry-batch` · `GET /api/image/batches/{batch_id}` |
| 素材库 | `POST/GET /api/image/library` · `DELETE /library/{asset_id}`（按账号隔离） |
| 我的 | `GET /api/image/assets/{id}` · `GET /api/image/quota` · `GET /api/image/health` |
| 管理员 | `/api/image/admin/{metrics,tasks,tasks/{id},users,quota,reload,reindex,replay-outbox}` |

### 离线评测与合规校验

```bash
PYTHONPATH=src python scripts/eval_image.py        # 召回 / Prompt 合规 / 首轮通过 / 时延
PYTHONPATH=src python scripts/bench_image.py       # 并发压测（零成本）
PYTHONPATH=src python scripts/check_image.py 主图.png --platform 淘系 [--fix out.png]
```
`check_image.py` 的像素级校验对 PNG **零依赖**（自带解码），JPEG/WebP 才需要可选的 Pillow。

起了真实服务后可以一条命令跑通全链路（建任务 / iterate 差额 / 幂等 / 素材库 / 拍立淘 / 批次 / 口径卡片）：

```bash
PYTHONPATH=src python scripts/smoke_image_service.py --app http://127.0.0.1:8021
```


---

## v7：面试评审后的修复与新增（生图 Agent）

评审发现的问题（P0/P1/P2）已全部处理，另按参考仓库 `04_Product_Image_Agent` 补了四项能力。

| 类别 | 变化 |
|---|---|
| **账号体系** | 统一到一张共享 `user` 表（SKU 与生图共用；`ACCOUNTS_SOURCE=legacy` 可回滚）；中台只做机器令牌（scope+部门），不含人员账号 |
| **出图粒度** | 改为「商品 + 颜色」（`product_colors` / `all_colors`），颜色 hex 锁进身份锁定；`slot_count`（图位数）与 `image_count`（出图张数）分离 |
| **幂等/配额/成本** | `Idempotency-Key` 幂等；配额改为**预留→成功结算→失败释放**；成本取 provider 真实计费（`external_cost_cny`） |
| **可靠性** | 执行期取数失败与创建期同口径（直接失败）；恢复看门狗（60s 扫租约过期任务）；取消区分「未调模型/已调模型」；SSE 轮询 3s；第三方生图并发上限 10 |
| **人工闭环** | 采纳（正样本）/驳回（负样本，需原因）接口 + 样本表；成本按 provider 成功计入，驳回不返还 |
| **图内文案** | 按图位的文案 + 字数表（主标语 ≤4 词 / 副文案 ≤7 词 / 角标仅数字），平台策略驱动（抖系主图禁文字） |
| **平台双套** | `platform_variant=true` 或 `platforms=[淘系,抖系]` 一次产出两套（硬冲突：淘系允许主图叠字、抖系禁止） |
| **variant 差异化** | `mode=variant` 输出 A/B 两套方向（版型优先 / 面料优先）；`iterate` 只重做主图 |
| **像素门禁** | 交付前按平台卡检查（白底纯度/主体占比/留白/尺寸/大小），主图 blocking、可 autofix、不过落 `blocked` 资产并可人工放行 |
| **部门隔离** | 普通账号只看到/只能出本部门商品（男装 / 女装含童装），管理员跨部门 |
| **前端** | 画廊用短签名 URL 直出（不再每图一次 401）、任务分页、SSE 断线重连、`scripts/ui_smoke_image.js` DOM 冒烟 |

新增接口：`POST /tasks/{id}/assets/{aid}/adopt|reject|unblock`、`GET /admin/samples`、`GET /admin/feedback`、`GET /assets/{id}?sig=&exp=`。

**评测口径**（`scripts/eval_image.py`，离线）：Recall@3、Prompt 合规率、像素门禁通过率、双平台差异率、variant 差异率、首轮通过率、时延。
出图**质量**评估后期由人工评测（采纳率/上架率/驳回原因），自动评测只覆盖结构与规则指标。

---

## v8：第二轮评审修复（生图 Agent）

第二轮评审的 12 类问题 + 一个新需求（参考图三源）已全部落地，另一批明确留到后期。

| 类别 | 变化 |
|---|---|
| **跨色串成品** | `iterate` 复用只对同一 `(颜色, 平台, 变体)` 生效；多色/双平台不再互相挂错成品 |
| **幂等** | 事务内先抢占 `image_request(pending)`：并发同 key 返回 409 + `Retry-After`，失败回滚后可重试 |
| **额度账** | 只依赖事务回滚撤销预留（不再二次释放）；派发失败保留 `queued` 交看门狗补投；结算按**使用量**占额度 |
| **看门狗** | 「启动超时」改为**租约**（每阶段续约）+ 重投封顶，正常长任务不会被误重投 |
| **取数效率** | 商品上下文 3 秒窗口合并成一次 MCP 调用（+60s TTL、50 个 id 分片）；部门闸前置到下载参考图之前 |
| **参考图三源** | 中台商品图 / 素材库（人工上传，按账号隔离）/ 拍立淘（销量 >100 的相似款前 5 个主图，无符合直接 422） |
| **iterate 差额** | 基础任务缺图位时按差额补齐，配额与结算用同一个张数（不再"预留 1 张、实际出 6 张"） |
| **文案口径** | 变体方向只进 Prompt 场景块、不进图内主标语；`copy_json` 落任务并回写中台 |
| **第三方韧性** | 限流/超时/5xx 退避重试（默认 2 次），真实发起次数记「发起量」；失败任务可单任务/整批重试 |
| **数据口径** | 5 张卡：使用量(+发起量/可用性) · 生图成功率 · 上架率 · 配额 · 在途+成本（单张成本） |
| **可解释** | 任务落 `warnings/blocked`；结算对账不一致 → `IMG_QUOTA_SETTLE_MISMATCH`；库结构降级 → `/health degraded` |
| **批次与首屏** | 整批取消/重试/批次汇总；管理端按部门筛选；画廊 20 张/页懒加载，首屏不再一次渲染几十张图 |

留到后期：Critic 视觉保真（vision 质检）、真实 provider 契约测试、出图质量的**人工评测口径**（需与业务部门统一）。

---

## v9：第三轮评审修复（P0 全部 + 指定 P1 + 全部 P2）

第三轮按功能逐项审查后，按 P0/P1/P2 汇总问题并全部处理（仅 P1-6 为记录取舍）。

| 类别 | 变化 |
|---|---|
| **P0 取消泄漏额度** | 取消"未调模型"的任务会真正退回预留额度并记一次失败使用量；`settled_at` 做结算权，取消/失败/交付只可能结算一次 |
| **P0 看门狗误杀** | 派发前 CAS 认领 + 排队宽限；只有"未被认领的 queued（超宽限）/ 租约过期的 running"才重投，`recoveries` 只在真正重投时 +1 |
| **P0 非方图必失败** | 像素门禁以**请求比例**为基准（平台卡只给阈值），autofix 按比例裁切；1:1/3:4/4:3/16:9 端到端都能交付 |
| **部分成功口径** | 交付少于计划时，详情/SSE/回写统一给「部分成功，预期 X 张，输出 Y 张」+ `IMG_SLOT_MISSING` |
| **门禁可见降级** | 非 PNG（且无 Pillow）不再静默放行：`IMAGE_COMPLIANCE_NONPNG=warn|block`，warn 交付但打 `IMG_GATE_SKIPPED` |
| **事务瘦身** | 参考图改两阶段（取字节 / 落库），建任务 = 读 → 取图（无事务）→ 短写事务，网络 IO 不再占写锁 |
| **拍立淘** | 每个商品用自身颜色选图；搜图输入同样校验素材归属；不排除自身商品（生产里销量最高多为其他商家同款，已记录理由） |
| **前端能力** | 图位数/出图张数分离、颜色多选、模式（标准 / A/B 差异化）、双平台、任务行"只改主图"按钮、部分成功与告警可见 |
| **中台治理** | `/products/context` 与 `/similar-by-image` 按调用方部门兜底过滤（`forbidden` / `filtered`） |
| **回写补偿** | outbox 后台自动重放；入队失败写 `IMG_OUTBOX_ENQUEUE_FAILED` 告警 |
| **口径与可观测** | 卡片随账号/部门过滤；在途以 DB running 为准；`/health` 反映库结构降级；eval 增加四比例/三源/部分成功指标 |

数据口径未变：**使用量** = 第三方成功生成张数；**发起量** = 调用发起次数；**生图成功率** = Agent 通过 ÷ 使用量；
**上架率** = 人工通过 ÷ 使用量；配额按使用量占额度。
