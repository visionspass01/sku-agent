# 电商 SKU 监管 Agent 项目简介（docker部署）




> **目录**
>
> [项目结构](#项目结构)：[1.1 项目定位](#11-项目定位) · [1.2 技术栈速览](#12-技术栈速览) · [1.3 关键后端目录树](#13-关键后端目录树) · [1.4 四层架构图](#14-四层架构图)
>
> [技术设计](#技术设计)：[2.1 四层记忆设计](#21-四层记忆设计l0--l1--l2--l3) · [2.2 检索向量方案](#22-检索向量方案) · [2.3 口径冲突裁决：文档元数据标记](#23-口径冲突裁决文档元数据标记) · [2.4 与 DSH 记忆机制的对应](#24-与-dsh-记忆机制的对应) · [2.5 Query 理解与重写设计](#25-query-理解与重写设计query_plan) · [2.6 上下文缓存（KV Cache）与提示词切分](#26-上下文缓存kv-cache与提示词切分) · [2.7 运行详情与成本控制](#27-运行详情与成本控制可观测性--demo-防漏量) · [2.8 异步读写分离与 CAS 哈希校验](#28-异步读写分离与-cas-哈希校验) · [2.9 电商闭环：订单 / 物流 / 售后 MCP](#29-电商闭环订单--物流--售后-mcp第三个-mcp-server) · [2.10 外部组件均为可选：MySQL / Redis / Qdrant / Embedding](#210-外部组件均为可选mysql--redis--qdrant--embedding)

## 项目结构

### 1.1 项目定位

本项目面向服装电商「多款、多色、多码」的 SKU 库存管理场景，构建一套**可演示、可落地的 AI 库存监管 Agent 应用**，并配套一套**轻量电商数据中台底座**。

- **业务目标**：把运营人员每周导出核对 SKU 报表、人工判断断码/积压/缺货/换季风险的工作，升级为「量化筛查 → 多 Agent 研判 → 校验合规 → 引用 SOP → 人工确认执行」的自动化闭环。
- **核心能力**：多维度量化初筛（线性回归趋势、销量基数、退货率、可售天数、断码、在途覆盖）；Supervisor-Worker 多 Agent 研判；校验 Agent 做合规与 SOP 检查；RAG 检索带引用溯源；ReAct 对话式问数/诊断/自动补货；高危补货 Human-in-the-loop 人工确认。
- **数据底座**：轻量数据中台完成 ERP/CRM/订单模拟接入、PII 脱敏、Airflow 模拟调度、dbt 式指标转换、指标口径治理（可用库存/安全库存/预计售罄/动销率），并通过 RESTful API 向 Agent 提供数据服务。
- **权限与安全**：管理员/部门用户两种账号；男装、女装部门数据隔离；SOP 按「通用 + 部门内部」分权；登录 Token、安全记录（近 4 小时登录 IP/时间）、操作审计。
- **两种运行形态**：代码默认值面向**本机零依赖**——SQLite + fakeredis + 内存哈希向量，`PYTHONPATH=src uvicorn app.main:app` 直接跑；公开演示实例跑**容器化同构栈**，两者只靠环境变量切换（组件开关与降级见 2.10）。
- **演示实例（4 核 / 4G，Ubuntu 22.04 + Docker Compose）**：

| 层 | 演示实例取值 |
|---|---|
| 关系库 | `mysql:8.0.39`，`sku_demo`（业务）+ `platform`（中台）两个库 |
| 向量库 | `qdrant/qdrant:v1.12.4`，集合 `kb_chunks_512`（67 点，512 维 Cosine） |
| 缓存 | **fakeredis**（未启用 redis 容器：缓存退化为不缓存，Stream 队列与分布式锁为进程内实现） |
| Embedding | 配了 `tencent` 后端但未填 Key → **降级为 512 维哈希向量**（`/api/health.embedding.degraded=true`）；补 Key 后集合切到 `kb_chunks_1024` 重建 |
| LLM | DeepSeek `deepseek-flash`，真实 Key，工具调用与 SSE 流式输出均可用 |
| 入口 | `nginx:1.27-alpine`：公网只开 80 / 443 / 500；443 只做 `301 → http`（浏览器默认跳 https 时的兜底，自签证书带 IP SAN），`/api` 与静态页反向代理到 app，mysql / qdrant / app 端口均不发布 |
| 其它 | checkpointer 用 SQLite；定时巡检默认关闭（`SCHEDULER_ENABLED=false`） |

- **演示数据**：演示实例当前 300 SKU / 70,212 条流水 / 47 个异常 SKU / 14 篇知识库（67 个片段）/ 120 订单 / 100 物流 / 14 售后工单；本机重新初始化会得到同量级的随机样本（流水 6.9 万级）。数字以 `/api/health` 与库内 `COUNT(*)` 为准，不写估算值。
- **演示入口与账号**：看板 `/` 聚合单 SKU 研判、批量巡检（实时进度 / 可终止 / 简报）、SOP 检索、SKU 快速定位、AI 助手对话与补货人工确认；`/agents.html` 为 Agent 目录页、`/about.html` 为本篇文档。账号 `admin / hhw123`（管理员）、`worker1 / hhw111`（男装）、`worker2 / hhw222`（女装），未登录为只读浏览。

### 1.2 技术栈速览

| 技术 | 用途 | 版本 / 说明 |
|---|---|---|
| Python | 服务端语言 | 3.11 |
| FastAPI | Web 框架 | 0.135.2（Agent :8000，数据中台 :8001） |
| Uvicorn | ASGI 服务器 | 0.42.0 |
| LangGraph | 状态图 / Agent 执行引擎 | 1.2.19（Supervisor-Worker、Skill 子图、create_react_agent） |
| LangChain | LLM 工具链 | langchain-core 1.7.3 / langchain-openai 1.7.3（Tools / Tool Calling） |
| Pydantic | 数据校验 / 结构化输出 | 2.12.5 |
| MCP Python SDK（FastMCP） | Agent ↔ ERP/拍立淘工具桥接 | 1.30.0（memory/stdio/direct 三种传输）；**ERP 与拍立淘均为本地 Mock 数据源**：ERP 读 SQLite 模拟库，拍立淘读 `competitor_listing` 竞品表，真实环境替换为拍立淘图片检索 API / 企业 ERP |
| SQLAlchemy | ORM / 数据访问 | 2.0.25（联合索引 stat_hour + sku_id） |
| SQLite | 本地存储 | 本机默认（`data/sku_demo.db` + `data/platform/platform.db`）；演示实例改用 MySQL，`DATABASE_URL` 一键切换 |
| MySQL 8 | 生产存储（可选） | 演示实例：`mysql:8.0.39` 容器，`sku_demo` + `platform` 两库 |
| Redis / fakeredis | 缓存、Stream 队列、分布式锁 | redis-py + fakeredis 2.40（TTL 随机抖动）；演示实例未起 redis 容器 → fakeredis，缓存退化为不缓存、Stream 队列与锁为进程内实现 |
| APScheduler | 定时任务 | 3.11.3（每小时筛查 + 每日 09:00 全量巡检） |
| rank-bm25 | 关键词召回 | 0.2.2（RAG 混合检索） |
| NumPy | 回归/向量计算 | 1.26.4（趋势斜率/R²、哈希向量余弦） |
| httpx | HTTP 客户端 | 0.28.1（Agent 调用数据中台、MCP 客户端） |
| DeepSeek | LLM | deepseek-flash（带 Tool Calling；关闭思维链降低延迟） |
| 四层记忆 | 上下文工程 | L0 工具契约 / L1 事实卡+工作集 / L2 AGENTS.md+Skills / L3 分析档案（见 2.1） |
| Query 理解与规划 | 先解析再取数 | 确定性 query_plan（意图/实体/时间窗/口径/置信度/plan_id，见 2.5） |
| 上下文缓存 | 成本与延迟 | 静态前缀 + 易变内容后置，扣住 DeepSeek 自动前缀缓存（见 2.6） |
| 并发与一致性 | 写入安全 | `asyncio.to_thread` 读写分离 + sha256 CAS / 原子写（见 2.8） |
| 口径冲突治理 | 制度一致性 | 文档 front-matter 元数据标记（`authority` / `key_facts`）确定性裁决（见 2.3） |
| Airflow（模拟） | 数据调度 | Python 模拟 DAG：依赖/重试/运行日志 |
| dbt（式转换） | 数据转换 | Python 实现 stg → dim/fct → mart 指标宽表 |
| Qdrant（可选） | 向量库 | qdrant-client 1.12；演示实例 `qdrant/qdrant:v1.12.4`，集合 `kb_chunks_512`（67 点，512 维；本地可用 `QDRANT_URL=local` 模式） |
| 腾讯云 TokenHub（可选） | Embedding | kinfra-text-embedding-0.6b 1024 维 / 4b 2560 维，OpenAI 兼容 /v1/embeddings |
| Docker（可选） | 部署 | Dockerfile + docker-compose（中台 + Agent + MySQL 8.0 + Redis 7 + Qdrant；可选组件与降级见 [DEPLOY.md](DEPLOY.md)） |

### 1.3 关键后端目录树

```text
sku-agent-demo/
├── src/app/                                 # Agent 服务（FastAPI :8000）
│   ├── main.py                          # 应用入口：生命周期 / MCP / 定时任务 / KB 索引
│   ├── config.py                        # 配置中心：LLM/阈值/缓存/队列/部门/认证/中台
│   ├── auth.py                          # 登录、HMAC Token、部门作用域、安全记录
│   ├── llm.py                           # DeepSeek ChatOpenAI 封装 + 部门归因统计
│   ├── scheduler.py                     # APScheduler：小时筛查 / 每日巡检推送
│   ├── api/
│   │   ├── routes.py                    # 指标/SKU/异常/研判/巡检/RAG/队列接口
│   │   ├── chat_routes.py               # ReAct 对话 + 补货审批（HITL）接口
│   │   └── schemas.py                   # 请求 / 响应模型
│   ├── agents/
│   │   ├── graph.py                     # 主图：Supervisor → Workers → Skill → Reasoner → Verifier
│   │   ├── supervisor.py                # LLM 任务拆解与 Send 并行分发
│   │   ├── workers.py                   # ERP / 拍立淘 / RAG 数据采集 Worker
│   │   ├── reasoner.py                  # LLM 生成结构化处置预案
│   │   ├── verifier.py                  # 规则 + LLM 双轨校验（合规 / SOP）
│   │   ├── chat_agent.py                # LangGraph create_react_agent + 四层记忆组装
│   │   ├── chat_store.py                # 多轮会话存储（按账号隔离）
│   │   ├── query_plan.py                # 查询理解与规划：intent/实体/时间窗/置信度/plan_id
│   │   ├── skills/                      # L2 Skills（按需加载的 5 个操作流程 Markdown）
│   │   └── prompts/                     # 系统提示词（外置 Markdown）
│   ├── memory/                          # L0-L3 记忆实现
│   │   ├── result_store.py              # L0：result_id/as_of/summary + artifact 落盘 + 取数台账
│   │   ├── tool_contract.py             # L0：工具装饰器（统一补齐契约字段）
│   │   ├── prune.py                     # L0：4096/2048/512 三档剪枝兜底
│   │   ├── session_memory.py            # L1：<analysis-facts> 事实卡 + <working-set> 实体表
│   │   ├── workspace.py                 # L2：AGENTS.md 基线 + Skills 目录/按需加载
│   │   └── org_memory.py                # L3：analysis_result + data/analyses/<部门>/ 档案
│   ├── tools/
│   │   └── platform_tools.py            # LangChain Tools：封装中台 API + 部门越权拦截
│   ├── skills/
│   │   ├── inventory_judge.py           # Skill 子图：库存研判
│   │   └── replenishment.py             # Skill 子图：补货测算（安全库存/MOQ）
│   ├── pipeline/
│   │   ├── screening.py                 # 多维度量化筛查
│   │   ├── snapshot.py                  # ERP 流水 → 小时快照表（联合索引）
│   │   ├── patrol.py                    # 巡检执行器：Stream 消费 / 进度 / 终止 / 简报
│   │   ├── progress.py                  # 实时进度注册表 + 协作式终止信号
│   │   ├── metrics.py                   # 指标卡（分部门、LLM/队列分归属）
│   │   └── ...
│   ├── cache/
│   │   ├── ttl_cache.py                 # TTL 随机抖动旁路缓存
│   │   ├── stream_queue.py              # Redis Stream 幂等队列
│   │   └── lock.py                      # 分布式锁
│   ├── db/
│   │   ├── models.py                    # SQLAlchemy 模型（含巡检日志/会话/认证事件）
│   │   ├── engine.py / repos.py / seed.py
│   ├── rag/
│   │   ├── loader.py / chunker.py       # 清洗 + 语义分块 + 元数据
│   │   ├── vectorizer.py / bm25.py      # 哈希向量 + BM25
│   │   ├── hybrid.py                    # 混合召回 + Rerank + 部门过滤
│   │   ├── index.py                     # 增量索引（sha256 指纹）
│   │   └── skill.py                     # RAG Skill 子图
│   ├── mcp_servers/
│   │   ├── erp_server.py                # FastMCP ERP 工具（7 个，Mock ERP 数据）
│   │   └── pailitao_server.py           # FastMCP 拍立淘工具（3 个，本地模拟竞品数据）
│   └── mcp_client/
│       └── client.py                    # MCP 客户端（memory/stdio/direct + 缓存）
│
├── web/                                     # 前端页面（原生 JS，无构建）：SKU 监管 Agent 演示页（index.html）+ Agent 目录页（agents.html）+ 项目简介（about.html ← /about.md）
│
├── src/data_platform/                       # 轻量数据中台（FastAPI :8001）
│   ├── api/
│   │   ├── main.py                      # 数据服务入口
│   │   ├── routes.py                    # 库存/销量/在途/CRM/订单/指标/审批/调拨
│   │   └── cache.py                     # Redis TTL 抖动缓存
│   ├── ingest/
│   │   ├── airflow_mock.py              # 模拟 Airflow：DAG / 重试 / 运行日志
│   │   ├── sync_erp.py / sync_crm.py / sync_orders.py
│   │   └── common.py                    # PII 脱敏（姓名/手机号/收货人）
│   ├── dbt_like/
│   │   ├── models.py                    # stg → dim/fct → mart 转换实现
│   │   └── run_transforms.py            # 模拟 dbt run + 数据版本刷新
│   ├── governance.py                    # 指标口径注册 + 脱敏规则 + Prompt 上下文
│   ├── mock_sources.py                  # ERP/CRM/订单模拟源快照
│   ├── db.py                            # 中台 ORM 模型（raw→mart / 审批 / ETL 日志）
│   └── config.py
│
├── scripts/
│   ├── init_db.py                       # SKU 演示库初始化（300 SKU × 30 天）
│   ├── platform_bootstrap.py            # 中台：模拟源 → 入湖 → dbt 转换
│   ├── demo_single.py / demo_chat.py    # 单 SKU 研判 / 对话演示
│   ├── run_screening.py / run_patrol.py
│   └── eval_accuracy.py / update_kb.py
│
├── tests/                               # 1700+ 条离线测试（206 个测试文件；FakeLLM / MockTransport / fakeredis）
├── AGENTS.md                            # L2 工作区基线（角色/口径/取数纪律/记忆使用顺序）
├── data/kb/                             # 14 篇知识库（通用 10：9 篇 SOP + 1 篇历史案例；男装 2；女装 2）
├── data/artifacts/<会话>/               # L0 超限明细落地（jsonl，按 result_id 命名）
├── data/analyses/<部门>/                # L3 组织记忆档案（Markdown + front-matter）
├── docker/                              # Dockerfile + docker-compose（中台/Agent/MySQL/Redis）
├── docs/db/schema.mysql.sql
├── README.md
└── docs/UI_GUIDE.md
```

### 1.4 四层架构图

```text
┌─────────────────────────────────────────────────────────────┐
│            Web 看板（web，:8000/）                    │
│  指标卡 · 单SKU研判 · 异常列表 · 批量巡检 · SOP检索           │
│  SKU快速定位 · AI助手(ReAct对话) · 补货单列表(HITL)          │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP（Bearer Token / 登录·部门隔离）
┌───────────────────────────▼─────────────────────────────────┐
│              API 层（FastAPI :8000）                         │
│  routes.py：指标/SKU/异常/研判/巡检/RAG/队列                │
│  chat_routes.py：/api/chat · /api/actions（审批）           │
│  auth.py：HMAC Token · 部门作用域 · 安全记录                 │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│              Agent 核心层（LangGraph + LangChain）           │
│  ┌──────────────┐  ┌───────────────────────────┐            │
│  │ 巡检主图      │  │ ReAct 对话 Agent           │            │
│  │ Supervisor   │  │ create_react_agent         │            │
│  │  → Workers   │  │ 多轮会话 · Tool Calling   │            │
│  │  → Skill子图 │  │ 治理元数据注入 Prompt      │            │
│  │  → Reasoner  │  └────────────┬──────────────┘            │
│  │  → Verifier  │               │                            │
│  └──────┬───────┘               │                            │
│         │ LangChain Tools（platform_tools.py，含越权拦截）    │
│         ▼                       ▼                            │
│  Skill 子图：库存研判 / 补货测算    RAG：分块→混合召回→Rerank  │
│  MCP 客户端：ERP(7)/拍立淘(3)      Redis：TTL缓存/Stream/锁   │
│  （ERP 与拍立淘均为 Mock 数据源，真实环境替换为线上 API）     │
└───────────────────────────┬─────────────────────────────────┘
                            │ REST + X-Internal-Token
┌───────────────────────────▼─────────────────────────────────┐
│         数据中台（FastAPI :8001）                             │
│  接入层：mock_sources → sync_*（PII脱敏/幂等）→ Airflow模拟   │
│  计算层：dbt 式转换 stg → dim/fct → mart 指标宽表             │
│  治理层：可用库存/安全库存/再订货点/预计售罄/动销率口径       │
│  服务层：RESTful API + Redis TTL 抖动缓存 + ERP Mock 审批流   │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                    存储与外部依赖                           │
│  SQLite / MySQL(可选) · Redis/fakeredis · Qdrant(可选向量)  │
│  DeepSeek（chat + tool calling）· Embedding（腾讯，可选）   │
│  Docker（可选部署）· 任一外部组件不可用即自动降级           │
└─────────────────────────────────────────────────────────────┘
```

## 技术设计

### 2.1 四层记忆设计（L0 / L1 / L2 / L3）

对话类 Agent 的瓶颈不是「模型会不会答」，而是「上下文装不下、结论不可复现、跨会话记不住」。本项目把记忆拆成四层：**工具结果（L0）→ 会话（L1）→ 工作区（L2）→ 组织（L3）**，每层解决一种失效模式，越往下越稳定、越省上下文。

#### 2.1.1 总览

| 层 | 载体 | 生命周期 | 维护者 | 关键动作 | 解决的问题 |
|---|---|---|---|---|---|
| **L0 工作记忆** | 工具返回（`result_id`/`as_of`/`summary`/`artifact_path`）+ `tool_result` 表 + `data/artifacts/` | 单次调用 → 会话 | 代码（`src/app/memory/result_store.py` 装饰器） | 聚合优先、>50 行只回 summary、回答引用 result_id | 明细撑爆上下文；数字无法复核 |
| **L1 会话记忆** | `<analysis-facts>` 事实卡 + `<working-set>` 实体表 + 最近 12 条原文 | 会话生命周期 | 每轮自动（`session_memory.py`） | 压缩前抽事实卡、每轮追加实体表 | 长对话忘事、指代解析失败 |
| **L2 工作区记忆** | 根 `AGENTS.md` 基线 + `src/app/agents/skills/*.md` | 仓库级（随版本发布） | 研发/运营人工维护 | 基线常驻注入、Skills 按需 `load_skill` | 规则反复丢失；一次性塞满提示词 |
| **L3 组织记忆** | `analysis_result` 表 + `data/analyses/<部门>/*.md` | 长期（跨会话/跨周） | 自动沉淀 + 人工留档 | 检索档案而非回忆对话；部门隔离 | 上周的分析这周从零重做 |

#### 2.1.2 L0 工作记忆：数据工具契约

**问题**：问「近 30 天全部订单明细」时，一次工具调用就能返回上千行，直接把上下文打满，而且回答里的数字无从复核。

**做法**（`src/app/memory/result_store.py` + `tool_contract.py`，所有 12 个中台工具统一套一层装饰器）：

1. **默认聚合优先**：系统提示里写明「优先 count/sum/avg/top-N，单次明细 ≤ 50 行」；
2. **工具级截断优先**：超过 `memory_max_inline_rows`（默认 50 行）时，工具只回 `summary`（确定性聚合摘要）+ `artifact_path`（明细落 `data/artifacts/<会话>/<result_id>.jsonl`）；
3. **每条结果带 `result_id` 与 `as_of`**，并登记到 `tool_result` 台账（谁、何时、哪个工具、多少行、是否截断）；
4. **回答必须引用 `result_id`**：没有 result_id 的数字只能表述为「推断，未取数」；
5. **通用剪枝仅兜底**：把历史工具结果回放进 prompt 时按 4096 / 2048 / 512 三档剪枝（`src/app/memory/prune.py`），只在工具级契约失效时生效。

工具返回示例（小结果直接内联，大结果转 artifact）：

```json
{
  "sku_id": "ST-13-C1-M", "available": 42, "on_hand": 120, "locked": 8,
  "result_id": "res_1a2b3c4d5e", "as_of": "2026-03-01T10:00:00",
  "summary": "get_inventory：sku_id=ST-13-C1-M；available=42；on_hand=120；locked=8",
  "truncated": false
}
```

```json
{
  "status": "all", "row_count": 320, "truncated": true,
  "items": [{"approval_id": "appr-1"}, {"approval_id": "appr-2"}, {"approval_id": "appr-3"}],
  "preview_rows": 3,
  "artifact_path": "data/artifacts/<session>/res_9f8e7d6c5b.jsonl",
  "result_id": "res_9f8e7d6c5b", "as_of": "2026-03-01T10:02:11",
  "summary": "list_pending_approvals：…；明细 320 行超过 50 行内联上限，已转存 artifact_path，请用聚合值与 result_id 作答"
}
```

#### 2.1.3 L1 会话记忆：事实卡 + 工作集

**问题**：`chat_history_limit=12` 一旦滚动，早期结论就丢了；用户说「这个款」「刚才那个尺码」时模型无从对应。

**做法**（`src/app/memory/session_memory.py`）：

- **`<analysis-facts>` 事实卡**：历史消息数超过 12 条或估算 token 超过阈值时，把历史里「带 result_id 的取数事实 + 带数字的结论短句」抽成结论骨架（**不含流水行**），存 `chat_memory.facts_card`（带 `facts_version` 与内容 hash，内容不变不重写）。默认**纯规则抽取**（`memory_facts_use_llm=False`），保证同输入同卡片、可复现。
- **`<working-set>` 实体表**：每轮从用户输入里确定性抽取 SKU / 款号 / 尺码 / 颜色 / 品类 / 指标 / 时间窗，与会话已有实体**并集**合并（记录 first_seen / last_seen / hits），存 `chat_memory.working_set`，解决指代。
- **压缩策略**：保留最近 `chat_history_limit` 条原文（`memory_retain_ratio=1.0` 等价于全保留原文），更早的消息只以事实卡形式存在。

```text
<analysis-facts version="3" as_of="2026-03-01T10:05:00" rule="deterministic">
- 可用库存 42 件，预计 3 天售罄。
- [get_inventory] available=42，on_hand=120，locked=8（res_1a2b3c4d5e）
</analysis-facts>

<working-set version="4" updated_at="2026-03-01T10:05:00">
SKU: ST-13-C1-M, ST-13-C1-L
款号: ST-13
尺码: M, L
品类: 男装
关注指标: 可用库存, 售罄天数, 预定
时间窗: 近7天
</working-set>
```

**补充：用户偏好画像（L1 的「人」维度）**

四层记忆补上"这个人怎么用"：`user_profile` 表存 `prefs`（输出偏好 / 口径偏好 / 默认时间窗）与 `watch_skus`（关注清单，≤20 个），`GET/PUT /api/memory/profile` 只允许本人读写、字段白名单过滤（不把任意 JSON 塞进提示）。每轮对话注入系统提示（≤200 字），与"当轮要求"冲突时以当轮为准。

边界：偏好画像**不存**对话原文（那是 `chat_message`），**不存**结论档案（那是 L3 `analysis_result`）——三者各自解决"跨会话的哪一半问题"。

#### 2.1.4 L2 工作区记忆：AGENTS.md + Skills

- **基线常驻**：项目根 `AGENTS.md`（角色、数据口径、取数纪律、记忆使用顺序、权限与动作）每轮注入系统提示，约 2.1k 字符（注入上限 3000 字符）；它只放「跨会话不变」的约定，变更走代码评审。
- **Skills 按需加载**：`src/app/agents/skills/` 下 5 个操作流程（`sku-screening`、`size-gap-diagnosis`、`overstock-review`、`restock-calc`、`weekly-ops-review`）；系统提示里只放「名字 + 一句话适用场景」（<300 字），模型判断需要时再调 `load_skill` 拉全文，避免一上来塞满上下文。

#### 2.1.5 L3 组织记忆：分析档案

- **写**：结论可沉淀为 `analysis_result` 行 + `data/analyses/<部门>/YYYYMMDD-<slug>.md`（front-matter 含 title/department/plan_id/metric_version/as_of/result_ids）；标题规范 `[部门] 主题 - 时间窗 - 结论摘要`。对话里由 `save_analysis` 工具触发，也支持 `POST /api/memory/analyses` 手动留档。
- **读**：`search_analyses` 用 BM25（与 RAG 同一套中文 2-gram 分词）在「标题+问题+结论+标签」上检索，**先按部门过滤再算相关度**；返回摘要不含对话全文。跨会话复用的是结论，不是聊天记录。
- **`@session` 引用**：消息里写 `@session 男装 周度复盘 断码` 时只注入「讨论脉络」（标题+结论+关键指标+证据 result_id，最多 3 条），不重放原对话。
- **权限即隔离**：档案 `department` 与账号部门作用域一致（女装账号可见女装+童装），管理员可全量；越权读取返回「无权限」。

#### 2.1.6 记忆的两种载体：事实卡 vs 实体表（都属 L1）

| 维度 | 事实卡 `<analysis-facts>` | 实体表 `<working-set>` |
|---|---|---|
| 记的是 | **说过什么结论**：历史里带 `result_id` 的取数事实 + 带数字的结论短句（如「可用库存 693（res_xxx，as_of 2026-11-04）」） | **聊到了哪些对象**：SKU、款号、尺码、颜色、品类、关注指标、时间窗，附带 `first_seen / last_seen / hits` |
| 解决什么 | 历史压缩与结论复用：超过阈值后早期原文被它替代，回答"刚才那个结论"时不必重放对话 | 指代解析：把"这个款 / 它 / 刚才那个尺码"还原成具体 SKU，也给 `query_plan` 做实体回填 |
| 怎么生成 | 规则抽取（`build_facts_card`），内容 hash 幂等，`facts_version` 递增 | 确定性正则抽取 + 并集合并（只追加），`working_version` 递增 |
| 含不含数值 | 含（但只是结论骨架，不含流水行） | 不含数值，只含实体键 |
| 何时失效 | 新取数覆盖旧结论 → 版本递增 | 会话切换即重置；实体可长期累积在会话内 |

一句话：**事实卡记录"结论"，实体表记录"对象"**。前者省 token，后者防误解。两者都已经常驻在发送框下方（黄标「当前事实卡」+ 紫标「实体表（工作集）」，各带版本号与复制），不用等回答展开就能看到当前会话"记得什么结论、认得哪些实体"。

### 2.2 检索向量方案

- **默认（零外网依赖）**：`src/app/rag/vectorizer.py` 的 512 维字符 n-gram 哈希向量（中文 1–3gram、md5 取桶、亚线性 TF、L2 归一化），写进 `kb_chunk.vec`（SQLite `LargeBinary`），进程内 NumPy 余弦 + `rank-bm25` 双路召回 → **RRF 融合（k=60）** → 启发式 Rerank（短语覆盖 / 标题命中 / 版本新鲜度 / 元数据匹配）。这一路径**永远保留**：本地开发、离线演示、任何外部组件故障时都靠它兜底。
- **可选 1：向量库**。`VECTOR_STORE=qdrant` 时向量写入 Qdrant（collection 名带维度，如 `kb_chunks_1024`；payload 只存元数据不存正文；SQLite 仍是正文与元数据权威源，集合可随时重建）。`QDRANT_URL=local` 可直接用 qdrant-client 官方本地模式，**本机不起容器也能跑同一套代码**。
- **可选 2：真实 Embedding**。`EMBEDDING_BACKEND=tencent` 时用**腾讯云 TokenHub**（OpenAI 兼容 `POST /v1/embeddings`）：`kinfra-text-embedding-0.6b`（1024 维，0.5 元/百万 tokens，适合"大规模召回/延迟敏感"）或 `4b`（2560 维）。接口约定：单条 ≤2000 字符、单批建议 ≤128 条、维度由模型固定（不支持 `dimensions`）、429 带 `Retry-After`、401/403 不重试。
- **降级阶梯**（每一层都可独立失败）：Embedding 失败 → 本次查询退回哈希余弦；Qdrant 失败 → 内存检索；Redis 失败 → 不缓存；任何一层降级都会写进 `/api/health` 并带原因。
- **检索评测**：`scripts/eval_rag.py`（题集 `data/eval/rag_golden.jsonl`，38 条，含 8 条口语化改述）给出 doc 级 **Recall@1/3/5、MRR、nDCG@5**。当前基线（哈希 + 内存，默认 RRF 融合）：Recall@1 **100%**、Recall@3 **100%**、Recall@5 **100%**、MRR **1.000**、nDCG@5 **1.000**；消融对照 bm25 92.1% / vector 94.7% / rrf 100%（`--ablation`）。Qdrant（同一套哈希向量）与内存结果**完全一致**（候选数 ≥ 语料块数），这正是"换存储不换语义"的验收标准。换 embedding / 改融合权重前后都跑这套题集做回归。
- **成本量级**：索引 67 块 ≈ 3.9k tokens ≈ **0.0000020 元**；每次检索 2 条短文本 embedding ≈ 30 tokens ≈ **0.000015 元**（TTL 缓存后更低）。所以"要不要上真实 embedding"的决策依据不是费用，而是**中文语义召回质量**与工程可维护性。


### 2.3 口径冲突裁决：文档元数据标记

**问题**：同一条口径常常有多份规定（中台治理口径、部门细则、通用 SOP、历史案例）。让模型「综合一下」的后果是给两个互相矛盾的阈值，或者折中出一个谁都没写过的数字。

**做法：把口径从「正文里的自然语言」提升为「文档元数据」**（`src/app/rag/loader.py`）。每篇知识库文档在 front-matter 里声明：

```text
---
doc_id: sop-clearance-women
department: 女装
authority: dept            # governance > dept > general > case
scope: dept
version: v0.9
key_facts: 触发条件|clearance_trigger_days=45天; 折扣阶梯|clearance_min_discount=6折
---
```

- **`authority` 决定谁优先**：`governance`（中台治理口径）> `dept`（部门细则）> `general`（通用规则）> `case`（历史案例）；
- **`department` 参与可比性闸门**：问题属于哪个部门，就只和「该部门 + 通用」的口径比较；别部门的细则只能表述为「若问的是 X 部门则为 Y」，不能当成已采用的口径；
- **`key_facts` 是可比较事实**：`小节|键=值`，分块时按小节写进块元数据（`src/app/rag/chunker.py`），不相关的小节不携带口径。

**裁决是确定性的**（`src/app/rag/conflict.py`）：

1. 只读 `key_facts`，**不解析正文里的数字**——正文可以写「约 45 天」，元数据必须是 `45天`；有了结构化事实，冲突才可能被解出来，而不是让模型在两个数字之间折中；
2. 同一口径的多个声明按权威度取**一条**，返回赢家 / 输家 / 依据，面板直接展示「采用 A（部门细则 v0.9）· 不采用 B（通用规则 v1.2）」；
3. 语料**没填** `key_facts` 时返回 `conflict_check=unavailable`，明说「没做比对」，**绝不说「没有冲突」**；只有单一来源时给 `single_source_keys`，提示「未做交叉验证」；
4. 同一文档内部前后矛盾单独报「语料不一致」，不给取舍结论——那是语料要修，不是系统要裁决。

**为什么不用向量相似度判冲突**：相似度高的两段话可能说的是一件事（相关），也可能说的是对立的两件事（冲突），阈值划不开；把「哪个字段、谁更权威」写进元数据，判断就退化成一次排序。

**评测**：题集 `data/eval/rag_conflicts.jsonl` 25 条（部门覆盖通用、跨部门不可比、缺事实只能报「未比对」、文档内矛盾四类），`scripts/eval_rag.py --conflicts` 当前 25/25。新增口径时要同步两处：文档 `key_facts` + 题集里的同义问法变体。

### 2.4 与 DSH 记忆机制的对应

| DSH 机制 | 本项目等价实现 | 落点 |
|---|---|---|
| `dsh-compaction-basic`（retainRatio） | `memory_retain_ratio` + `chat_history_limit` + 事实卡替代早期消息 | `session_memory.py` / `config.py` |
| `dsh-compaction-tool-result-pruner`（4096/2048/512） | `memory_prune_levels` 三档剪枝（仅兜底） | `prune.py` |
| `dsh-session-reference`（引用条目上限） | `@session` 只带脉络、最多 3 条 | `org_memory.analysis_context` |
| `dsh-session-query-sqlite`（FTS5 unicode61 中文不切词） | 档案检索改用 BM25 + 中文 2-gram，先部门过滤 | `org_memory.search_analyses` |
| AGENTS.md 基线注入 | 根 `AGENTS.md` → `load_workspace_baseline()` | `workspace.py` |
| Skills 按需加载 | `list_skills` / `load_skill` 两个工具 | `workspace.py` + `platform_tools.py` |

设计取舍：**工具级截断优先于通用剪枝**（少一次猜测，多一份可复现）；**事实卡优先于原文回放**（同样 token 装更多结论）；**档案检索优先于重读对话**（跨会话成本最低）。

### 2.5 Query 理解与重写设计（query_plan）

#### 2.5.1 先想清楚：重写不是万能药

| 用途 | 必要性 | 本项目的做法 |
|---|---|---|
| 会话内解析（意图/实体/时间窗/必需数据） | **必须** | 每次对话前确定性生成 `query_plan`，决定调哪些工具 |
| 检索改写（关键词扩展/同义扩展） | **需要** | 生成 `retrieval_query.keywords/expanded`，只用于 RAG 与档案检索 |
| 生成式整句改写（用 LLM 重写用户问题） | **不建议默认开** | `query_rewrite_llm=False` 默认关闭；开启后只写入 `generative_rewrite` 字段，**永不替换原句** |

原因：整句改写一旦把「近 7 天」改成「近 30 天」、把「可用库存」改成「现存库存」，回答看着通顺但口径已经错了，且没人能发现。结构化计划是**只追加**的，能被日志、UI 与档案复核。

#### 2.5.2 实现（`src/app/agents/query_plan.py`）

- **确定性优先**：SKU/款号/尺码正则、时间窗词表、指标词表、意图关键词优先级表，全部规则实现；LLM 只用于可选的检索改写。
- **检索关键词只收录「实体 + 命中业务词表」**：不做自由 n-gram 拼接（BM25 侧 `tokenize_zh` 会自行切 2-gram），避免「个款/款需」这类碎片污染召回。
- **指代回填**：本轮未给 SKU 但出现「这个款/它/刚才」时，用 L1 `<working-set>` 回填，并标注 `resolved_from` 与 `assumptions`（置信度按回填打折）。
- **权限前置**：先用账号部门判定 `allowed_departments`，命中越权品类直接 `blocked=true`（工具层还会二次拦截，双保险）。
- **可复现**：`plan_id` = 规范化 JSON 的 sha1（键排序、剔除 as_of/plan_id），同输入跨会话稳定；与 `as_of`、`metric_version`、`tool_fingerprints` 组成最小可复现三元组。
- **失败降级**：任何异常都返回可用计划，不阻塞对话（`degraded` 场景下按 lookup + 默认近 7 天处理）。
- **追问一次**：置信度 < 0.7 **且真的缺实体**时返回 `clarify`，同一缺口本会话只追问一次（`chat_query_plan.asked_clarify` 去重），避免无限澄清。口径解释（policy_qa）、批量巡检（patrol）、审批清单（approval）以及带部门/品类面的聚合问题不追问——那类问题不需要 SKU，问「要分析的 SKU」只是误导卡片。

```json
{
  "plan_id": "qp_9eee874acf33ba00", "schema_version": "1.0",
  "as_of": "2026-03-01T10:00:00", "metric_version": "2026Q1",
  "intent": "diagnose", "confidence": 0.86, "needs_clarify": false, "clarify": null,
  "entities": {"skus": ["ST-13-C1-M"], "styles": ["ST-13"], "sizes": [], "colors": [],
               "categories": ["男装"], "departments": ["男装"]},
  "time_range": {"label": "近7天", "start": "2026-02-22", "end": "2026-03-01"},
  "filters": {"warehouse": null, "channel": null},
  "required_data": ["inventory", "in_transit", "reservations", "orders", "metrics"],
  "retrieval_query": {"original": "ST-13-C1-M 是否面临断货风险，近7天",
                      "keywords": ["ST-13-C1-M", "ST-13", "断货", "库存"],
                      "expanded": ["男装", "售罄 天数 断货 风险"]},
  "assumptions": ["时间窗取近 7 天"],
  "resolved_from": "", "allowed_departments": ["男装"], "blocked": false,
  "tool_fingerprints": [{"table": "inventory", "fingerprint": "a1b2c3d4e5f6"}],
  "generative_rewrite": null
}
```

#### 2.5.3 两种接入方式

1. **MCP / LangChain 工具 `plan_query(question)`**：多轮模糊时 Agent 可主动调用，拿到同一份结构化计划再决定取数顺序。
2. **对话前置注入**：`POST /api/chat` 在调 LLM 前生成计划，把「意图/实体/时间窗/必需数据/假设/plan_id」以紧凑文本注入系统提示（`plan_brief()`），**只追加、不改写**用户原句。

#### 2.5.4 看板上的「Query 重写」开关

- AI 助手输入框上方有 `Query 重写` 复选框，选择状态存 `localStorage`（`skuAgent.queryPlan`），刷新后保持；
- 勾选后请求体带 `include_query_plan: true`，响应的 `query_plan` 会在**本轮用户气泡下方**渲染成计划卡片：意图、SKU/款号、尺码颜色、时间窗、必需数据、检索关键词、假设、`plan_id`、口径与 `as_of`；
- 置信度 <0.7 时卡片标黄并展示 `clarify` 追问；回答下方另有一行「本次回答引用的取数结果：res_xxx」，把 L0 契约显性化；
- 历史消息与切换会话**不会回放旧计划**（只存 `chat_query_plan`，默认不展示），避免把上一轮的解析结果误当成这一轮的。

#### 2.5.5 相关接口

| 接口 | 作用 |
|---|---|
| `POST /api/chat`（`include_query_plan`） | 对话；按开关回传本轮 `query_plan` / `clarify` / `cited_result_ids` / `memory` / `run_detail` |
| `GET /api/chat/{sid}` | 会话历史：每条 assistant 消息附带当轮的 `run_detail`（含事实卡快照） |
| `GET /api/memory/sessions/{sid}` | L1 快照：事实卡、工作集、最近 query_plan、最近取数台账、已记录轮次（仅会话所有者或管理员） |
| `GET /api/memory/analyses?q=` | L3 检索：BM25 + 部门过滤（无关键词回退最近档案） |
| `POST /api/memory/analyses` | L3 留档：写 `analysis_result` + Markdown 档案 |
| `GET /api/memory/analyses/{id}` | 读取档案全文（跨部门 404） |
| `GET /api/scheduler/status` | 定时巡检状态（是否启用 / 下次运行 / 上限），匿名可读 |
| `POST /api/scheduler/run-once?job=hourly\|daily` | 单次演练定时任务（管理员），走同一套 job 但不留定时器 |

### 2.6 上下文缓存（KV Cache）与提示词切分

**为什么单列一节**：一轮对话的输入 token 常在几千到上万（系统提示 + 工具 schema + 事实卡），DeepSeek 的自动前缀缓存会把**前缀命中部分按更低单价计费**，命中与否直接影响单轮成本与首 token 延迟。但它对前缀要求很苛刻：**请求开头只要有一个字符变了，后面的缓存就全部失效**。

**提示词按「稳定 → 易变」切三段**（`src/app/agents/prompts/chat_system.md`）：

| 位置 | 内容 | 变化频率 |
|---|---|---|
| 前缀（L1–L71） | 角色、工作规则、服装口径、L0 取数纪律、知识库引用规范、中台治理口径 | 只在改提示词 / 改口径时变（治理口径走 TTL 缓存 + 版本号，不逐轮重取） |
| 中段（L73） | `{memory_sections}`：L2 基线 + Skills、用户偏好、query_plan、L1 事实卡 + 工作集、L3 档案引用 | 每轮可能变 |
| 末尾（L75–76） | 「今天是 {today}；当前账号：{username}（{role_text}）」 | 每天 / 每账号变，但**只占最后两行** |

- **戳记一律后置**：日期戳 `今天是 …` 是模板最后一行；**提示词版本号不进正文**（它作为 `run_detail` 的 `prompt_version` 单独记录）。因此同一账号在同一天内，系统提示前缀逐字符一致，缓存能吃满。
- **动态内容为什么不放前面**：事实卡、工作集、query_plan 每轮都可能变，放前缀等于主动放弃缓存；把它们压到尾部后，前缀只随提示词版本变化。
- **反模式对照**：把「今天日期 / 用户名 / 事实卡」写进开头，每次请求前缀都不同，命中率≈0——这看起来只是排版，实际是成本和延迟的开关。

**可观测**：命中 token 从 `usage.prompt_cache_hit_tokens`（OpenAI 兼容层）或 `usage_metadata.input_token_details.cache_read`（langchain 新版）读取；单轮写进 `run_detail.llm.prompt_cache = {hit_tokens, hit_rate, note}`，进程级累计在 `LLMClient` 的 `prompt_cache`（`src/app/llm.py`）。注意别和看板「缓存命中率」卡片混读——那张卡是 Agent 旁路 TTL 缓存（工具结果缓存），两者的分母不同。

**怎么验证**：改一句前缀提示词 → `prompt_version` 变 → 下一轮前缀全变、`hit_tokens` 归零；恢复旧版本后命中量回升。整个过程只看 `/api/chat` 响应的 `run_detail`，不依赖外部工具。

### 2.7 运行详情与成本控制（可观测性 + Demo 防漏量）

#### 2.7.1 每轮「运行详情」：让结论可核验

AI 助手每条回答下方都有**证据链**与可展开的**运行详情**，数据来自 `chat_turn_log` 表（新表，`create_all` 直接生效，无需迁移）：每次对话单独落一份 `run_detail`，历史消息回看的是**当时**的事实卡快照，而不是当前状态。

```text
qp_9eee874acf33ba00 · diagnose · 置信度 86%
  →  get_inventory(res_1a2b3c4d5e, 2026-11-04) → get_sell_through(res_9f8e7d6c5b, 2026-11-04)
  →  事实卡 v3  →  耗时 12.5s（模型 12.0s / 工具 420ms）/ 5.1k tok
```

展开后分七段：① **查询计划**（意图/实体/时间窗/必需数据/假设/plan_id）② **时间轴**（按发生顺序列出「阶段 → 模型调用 → 工具」，并给出耗时构成 `模型 6.1s（3 次） · 工具 210ms（7 次） · 其它 1.4s · 整轮 7.8s`）③ **工具调用明细**（每次调用的入参、耗时、`result_id`、`as_of`、行数、是否截断与 artifact）④ **记忆**（本轮注入的事实卡全文 + 上一轮事实卡对比 + 工作集实体表）⑤ **模型与成本**（调用次数、输入/输出 tokens、每次模型调用的耗时与用途、耗时构成）⑥ **溯源自检**（`grounding.status` 与问题清单：哪些来源对不上、哪些数字找不到出处）⑦ **引用与权限**（result_id / chunk_id / artifact / 组织档案 / 部门作用域 / 失败步骤）。

**为什么单独画「时间轴」**：一次真实对话实测为「工具每次 15~37ms，但整轮 7.8s」。差在模型：工具返回后 LangGraph 会**再调一次模型**（决定下一步约 1.2s，生成长答复约 4.9s）。工具耗时只含「工具函数 + 契约封装（`result_id`/台账回写/必要时落 artifact）」，模型耗时单独登记为 `llm_chat`。不把两者拆开，看板上「16ms 的工具」和「等了好几秒的界面」就会互相矛盾。

配套还有「记忆面板」抽屉（`GET /api/memory/sessions/{sid}`）与「历史会话」下拉（`GET /api/chat/{sid}` 回放当轮详情），方便按时间线复盘。

演示动线（自上而下，30 秒）：

1. 发一句「ST-01-C1-M 当前可用库存」→ 提问下方出现**蓝标「Query 重写」卡片**（结构化计划），回答下方是**绿标「显示运行详情」**（证据链 + 默认展开的面板）；第 2 轮起「④ 记忆」里出现事实卡全文。
2. 两个勾选框的产物**各有归属标签**：蓝标 = 只由「Query 重写」产生；绿标 = 只由「显示运行详情」产生；黄标「当前事实卡」与紫标「实体表（工作集）」= 发送框下方常驻区（会话级事实卡 + 实体表，各带版本/触发原因/复制；紫块还可点「完整记忆」看取数台账）。取消勾选运行详情只收起绿色面板，证据链与两个常驻块仍保留。
3. 点「记忆面板」→ 弹窗给事实卡 / 工作集 / 最近取数台账；若当前没有会话，会列出最近会话让你挑一条查看。
4. 点「新会话」→ 开始新对话，**旧会话保留**；在「历史会话」下拉里选中即可回放旧问答 + 当时的运行详情与事实卡快照（只读回放，可继续追问）。

**为什么值得展示**：评审者关心的是「数字可信吗、上下文怎么省、权限边界在哪」。证据链把「计划 → 取数 → 记忆 → 回答」串成一条可追溯的线，比贴一大段日志更有说服力；原始工具 payload、system prompt 全文、内部 SQL 反而不展示（信息密度低且暴露实现细节）。

展示分级（也是本项目的默认取舍）：

| 级别 | 内容 | 理由 |
|---|---|---|
| 默认可见 | 证据链（plan_id / 每次取数的 result_id + as_of / 事实卡版本 / 耗时与 token） | 一句话证明「每个数字都能回溯到一次取数」 |
| 展开一次 | 事实卡全文 + 工作集实体表 + 上一轮事实卡对比 | 展示上下文工程：压缩的是流水，保留的是结论骨架 |
| 折叠备用 | 完整入参、返回摘要、artifact 路径、组织档案引用、权限命中/越权记录 | 体现可审计与部门隔离，被追问时随手展开 |
| 不展示 | system prompt 全文、工具原始 payload 全文、内部 SQL 指纹明细、客户字段 | 噪音大、暴露实现细节，且稀释"决策"这条主线 |

#### 2.7.2 实时进度与延迟分位（可观测性二期）

- **等待过程可视化**：对话改用 SSE（`POST /api/chat/stream`）——服务端在读记忆、规划、**每次模型调用开始/结束**、每次工具调用开始/结束各推一条事件（`stage` / `llm_start` / `llm_end` / `tool_start` / `tool_end`），前端渲染「已等待 3.2s · 当前：模型调用 #3 · 推理中 · 已 3.1s」并逐行列出「🧠 模型 #3 返回 4.9s · in 14.5k / out 1.1k tok · 生成答复」与已完成工具的 `result_id/as_of/耗时`。**模型等待不再被误算成工具耗时**：`tool_end` 之后若没有新事件，阶段文字会立刻切成「模型调用 #k · 已 X.Xs」，而不是停在上一个工具的「取数：xxx」。服务端每 1s 发一次 `tick` 作为计时权威来源，浏览器后台被降频也不影响。流式请求同样携带 `Authorization: Bearer`，仅在真正的网络/解析失败时降级回 `POST /api/chat`，并在进度卡上写明降级原因；401 会走登录失效流程而不是静默降级。
- **交互细节**：功能介绍按勾选动态显示（勾 Query 显示计划说明、勾运行详情显示证据链说明、都不勾就不显示、两个都勾则一行一个）；历史会话只保留最近 **24 小时**（`CHAT_RETENTION_HOURS`，列举时自动清理过期会话及其消息/运行详情/计划/记忆）；「自检」入口已从 UI 移除，能力保留在 `openDiagnostics()` 里。
- **延迟分位（P50/P95/P99）**：`src/app/observability/latency.py` 记录 6 类采样——`api`（接口耗时）、`tool`（单次工具）、`platform`（Agent→数据中台）、`llm`（巡检/研判的 JSON 模式调用）、`llm_chat`（**对话模型的每次调用**，用 LangChain 回调采样）、`chat_turn`（整轮对话），进程内保留最近 1000 次；`/api/metrics` 返回 `latency`，看板「缓存命中率」卡片第二行显示整体 P50/P95，点开可看分类明细（口径：不含每 3 秒轮询的 `/api/metrics`、`/api/health`）。
  - 性能说明（两处都修过，实测对比）：
    1. `PlatformClient` 复用同一个 `httpx.AsyncClient` 且 `trust_env=False`。此前每次新建 client（加读 Windows 系统代理）会让**单次取数从 6ms 膨胀到 1.4s**，工具 P50 一度到 5s；
    2. SQLite 默认 `journal_mode=delete + synchronous=FULL` 在 24MB 演示库上单次 commit 要 **400~1000ms**（每次工具调用都要写一条取数台账），已改为 `WAL + synchronous=NORMAL`（Agent 库与中台库都改），commit 降到 **1~2ms**。
    修复后实测：`tool` P50 1ms（缓存命中）/ 111ms（首次取数），`platform` P50 110ms，`chat_turn` P50 3.2s（LLM 两次调用为主）。
- **两层缓存口径（卡片为什么不再是 0%）**：卡片读的是 **Agent 进程内旁路缓存**（`TTLCache`，TTL 抖动 `TTL×(1+U(0,0.2))`）。一期的实现里只有 MCP 工具与 RAG 召回用它，对话取数走的是 REST 直连 → 命中/未命中都是 0，卡片就把"无采样"显示成了 `0%`。现在**对话工具只读 GET 也走这层缓存**（`cache_ttl_platform=120s`，写操作永不缓存），命中会在工具时间线里标 `✓ 命中`，于是同一 SKU 连问两次就能看到命中率与非零 P50/P95 下降；中台侧还有它自己的 TTL 缓存（响应里的 `cached` 字段），点卡片弹出的「缓存 & 延迟明细」会同时列出 Agent 层与中台层的命中次数/命中率与各自 TTL。无采样时卡片显示「暂无采样」而不是误导性的 `0%`。
- **断链自愈三件套**（页面与脚本版本不一致曾导致"勾选了却什么都不出现"）：
  1. `GET /` 与 `GET /agents.html` 由服务端渲染，把 `app.js`/`agents.js`/`style.css` 改写成 `?v=<内容哈希>`，从根上避免旧脚本；
  2. `/api/health` 返回 `assets`（当前资源哈希）与 `features`（后端能力自述，含 `run_detail`/`chat_stream`/`latency`）；
  3. 页面内置版本比对 + 「自检」面板：展示页面/脚本版本、关键函数是否存在、本地开关、DOM 计数、最近一次对话响应的键名与最近错误，并提供「探测后端一次对话」「重置本地开关并强刷」。

#### 2.7.3 看板指标口径（对话/巡检/缓存三层对齐）

三张卡片读的都是**后端进程级指标**，口径写清在卡片下方：

| 卡片 | 数据来源 | 覆盖的 UI 交互 |
|---|---|---|
| 缓存命中率 | `get_ttl_cache().stats`（Agent 旁路缓存，TTL 抖动） | 对话取数（只读 GET）、MCP 工具、RAG 检索（`/api/rag/search` 已接入）；中台层缓存单列在「缓存 & 延迟明细」里 |
| LLM 调用 / token | `LLMClient.stats` + 对话补登记（`record_usage(source="chat")`） | 对话（ReAct 每次工具循环）+ 巡检研判 + 校验 + 单 SKU 研判；明细里有「来源分布」（对话/巡检/其它）与分部门表 |
| 巡检队列 pending / 流长度 | `StreamQueue.stats()` + `runtime_state.active_chat_streams()` | 巡检（Redis Stream 队列）；对话是同步 ReAct + SSE 直推，**不进队列**，卡片上单独显示「对话进行中 N」 |

之前「聊了多次 LLM 仍是 0」的原因：对话 Agent 用的是自己 new 的 `ChatOpenAI`，不经过 `LLMClient`，所以全局统计里没有对话的调用；现已由 `chat_agent` 汇总 `usage_metadata` 后补登记（含部门归因与来源拆分）。

#### 2.7.4 定时巡检默认关闭（可一键开启）

定时巡检会**真实调用 LLM**（每小时 5 个 SKU、每日 09:00 共 50 个 SKU），为避免部署后无人访问仍持续消耗，默认关闭；需要时一行配置即可恢复：

- `SCHEDULER_ENABLED` 默认 **`false`**（`src/app/config.py`、`docker-compose.deploy.yml`、`deploy.env.example`、`deploy-no-compose.sh` 全部同步）；启动日志会明确提示「定时任务未启用（Demo 默认）」；
- **功能保留**：APScheduler + `hourly_job` / `daily_job` 代码不动，置 `SCHEDULER_ENABLED=true` 即恢复；也可以管理员调 `POST /api/scheduler/run-once?job=daily` 单次演练，或在看板点「批量巡检」（`POST /api/patrol`）手动全量巡检；
- 定时任务状态从 `/api/health` 的 `scheduler_enabled` / `scheduler_next_run` 直接可查（前端 `renderSchedulerBadge()` 代码保留，当前页面未挂载徽章元素），避免误以为后台在跑；
- 前端轮询在页面不可见（`document.hidden`）时暂停，恢复可见立即刷新一次，减少常开 Tab 的空转请求。

#### 2.7.5 工具调用可靠性 + 成本（可观测性三期）

延迟分位只回答"工具平均多快"，回答不了"哪个工具在失败、重试了几次、是不是超时"。因此新增两块指标：

| 指标 | 采集点 | 口径 |
|---|---|---|
| 每工具成功率 / 重试 / 超时 / P95 | `src/app/observability/tool_stats.py`，埋点在 `MCPHub._call_raw` 与 `tool_contract` | 按 `server.tool` 维度累计：成功率 = 成功次数 / 调用次数（含重试后成功）；重启清零 |
| LLM token 成本（元） | `LLMClient.cost()`，单价 `LLM_PRICE_INPUT/OUTPUT`（元/百万 tokens，可 `.env` 覆盖） | 全局 / 分来源（对话、巡检、校验、embedding）/ 分部门；只作量级参考，以合同价为准 |

- `/api/metrics` 新增 `tools` 与 `llm.cost`；看板「缓存 & 延迟明细」弹窗新增「③ 工具调用可靠性」表（调用 / 成功率 / P95 / 重试 / 超时），LLM 卡片 tooltip 带「估算成本」；
- 延迟分类新增 `mcp_call`（MCP 调用含重试）、`rag`（向量+BM25+rerank）、`embedding`（TokenHub 调用），与 `tool`（对话侧工具含台账回写）区分开；
- 每轮 `run_detail.llm.cost_cny` 直接给出本轮成本，演示时可以指着说"这一轮约 0.0x 元，其中 9 成在模型调用上"。

### 2.8 异步读写分离与 CAS 哈希校验

**问题一：写操作会卡住事件循环。** 对话走 SSE 长连接，一轮请求里要写会话库、写轮次日志、读写缓存与队列；只要有一次同步 IO 落在协程里，事件循环就被钉住，同进程其它会话的心跳、巡检队列一起被拖慢。

- **读写统一挪到 worker 线程**：`chat_routes.py` 的会话持久化、`stream_queue.py` 的队列收发、`ttl_cache.py` 的缓存读写、`chat_agent.py` 的记忆装配，全部用 `asyncio.to_thread` 包一层；请求协程只做编排与流式推事件，**读写分离**之后长连接不再被写库阻塞（这也是 SSE 能保持心跳的原因）。

**问题二：并发写同一份文件会静默覆盖。** 分析档案主文档（`data/analyses/<部门>/*.md`）由多次分析共同沉淀：两个人同一分钟对同一问题出结论，文件名会撞在一起，后写的直接覆盖前一份正文——DB 里两行都在，磁盘上只剩一份，而且没有任何日志。

- **CAS 写入**（`src/app/memory/analysis_merge.py`）：读时取文件 sha256 → **短锁内**校验哈希未变 → `atomic_write`（同目录临时文件 + `fsync` + `os.replace`）原子替换；**耗时的合并 / 模型调用不在锁内**，锁只保护"校验 + 替换"这一小段。
- **冲突不丢失**：哈希对不上（期间有人写过）→ 保留原快照、新快照、合并结果三份，并落 `pending` 队列；主文档保持原样，**绝不静默覆盖**，等人工或下一次运行再合并。
- **模型不许改事实**：LLM 只能改写「结论摘要」一段，必须保留原文全部数字与 `result_id`，否则整段拒收并回退到确定性合并；「各次分析结论」一节永远按原文渲染。
- **事实源在 DB**：`analysis_result` 行才是事实源，Markdown 只是投影——合并坏了可以从 DB 重渲染（`scripts/render_analysis_md.py`），取证材料不会丢。

**同一套「内容哈希」思路还用在别处**：`kb_doc.sha256` + 块集合指纹（`src/app/db/models.py`）→ 只重建变动文档的索引；静态资源按内容 sha1 注入 `?v=`（`src/app/static_assets.py`）→ 页面与脚本版本天然一致，不会出现"页面新、脚本旧"；写工具用幂等键（订单 + 原因 + 数量）→ 重复提交返回同一张单；`scripts/ensure_init.py` 按「表与行数」判断初始化 → 重启不重复灌数。

**怎么验证**：并发对同一问题跑两次分析 → 主文档只有一份 + 出现冲突快照 + `data/analyses/_merge_log.jsonl` 有两条记录；`data/analyses/_pending/` 里能看到待合并载荷。

### 2.9 电商闭环：订单 / 物流 / 售后 MCP（第三个 MCP Server）

库存/竞品/SOP 之外，运营最常问的是"这单到哪了、能不能退"。新增 `src/app/mcp_servers/order_server.py`（FastMCP，5 个工具）：

| 工具 | 说明 |
|---|---|
| `get_order_status(order_id)` | 订单状态 + 物流摘要 + 已建售后工单 |
| `list_orders(sku_id, days)` | 某 SKU 近 N 天订单（时间倒序） |
| `get_logistics(order_id)` | 承运商 / 运单号 / 状态 / 预计到货 |
| `get_return_risk(sku_id)` | 退货率 + 近期订单量 + 售后工单量（≥18% 判高风险，与筛查口径一致） |
| `create_after_sales_proposal(order_id, reason, qty)` | **写操作**：幂等键 = 订单+原因+数量，重复提交返回同一张工单；返回 `pending_confirmation`（人工确认语义，不自动退款） |

- 数据源：`mock_order` / `mock_logistics` / `after_sales_ticket` 三张表，`scripts/seed_orders.py` 从 `flow.order_id` 抽样生成 120 单（可复现）；
- 对话侧新增 4 个包装工具（`get_order_status` / `list_orders` / `get_logistics` / `create_after_sales`），内部做**部门权限校验**（看别的部门订单直接返回「无权限」）；写操作**绕过缓存**（`MCPHub.call(use_cache=False)`），幂等由服务端唯一键保证。

### 2.10 外部组件均为可选：MySQL / Redis / Qdrant / Embedding

默认值保持「零依赖可跑」（SQLite + fakeredis + 内存向量 + 哈希向量），每个外部组件都是一个**可选开关 + 一条自动降级**；打开哪个就多一分性能/语义上的能力，关掉或不可用也不会把对话拖成 500：

| 组件 | 开关 | 不开 / 不可用时的行为 |
|---|---|---|
| MySQL 8.0 | `DATABASE_URL=mysql+pymysql://…` | 默认 SQLite；切换后启动等数据库健康检查再初始化 |
| Redis 7 | `REDIS_URL=redis://redis:6379/0` | 退回 fakeredis：缓存当 miss（不缓存），`/api/health` 标 `redis(degraded)` |
| Qdrant 1.12 | `VECTOR_STORE=qdrant` + `QDRANT_URL` | 检索退回进程内内存余弦，`/api/health.vector_store.degraded` 带原因 |
| TokenHub Embedding | `EMBEDDING_BACKEND=tencent` + Key | 该次查询退回 512 维哈希向量，`/api/health.embedding.degraded` 带原因 |

三处跨栈细节都按**可移植**方式处理：① 中台 dbt 的 `iif()` 改成可移植 `CASE WHEN`（SQLite / MySQL 同一套 SQL）；② 初始化按**表与行数**判断（`scripts/ensure_init.py`，两种库通用、重启不重复灌数）；③ 缓存与向量库的所有外部调用都包了降级，任何一层失败只影响能力、不影响可用性。
