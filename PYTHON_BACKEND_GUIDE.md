Python 后端代码完全解析 — 从零理解 SKU 库存监管 Agent

> 本文档按逻辑阅读顺序逐模块解析 Python 后端代码。每个小节包含文件路径、代码规模、核心职责、关键函数与设计决策，并给出对应的测试文件。按顺序阅读即可从零理解系统如何运行；配合 `docs/ARCHITECTURE.md`（设计取舍）与 `docs/API.md`（接口契约）使用。

---

## 第1章 — 项目概览与架构

### 1.1 项目定位

本项目是基于 **LangGraph 多 Agent 编排 + MCP 工具协议 + RAG 检索增强** 的服装电商 SKU 库存监管智能体，服务于运营与供应链岗位。系统由 DeepSeek 驱动，通过 MCP 协议与内置的轻量数据中台交互，实现自然语言问数、单 SKU 研判、补货预案与人工审批、批量异常巡检、SOP 检索与跨部门口径裁决。

代码规模：`src/app/` 103 个文件 / 21,977 行，`src/data_platform/` 29 个文件 / 5,378 行，`tests/` 114 个文件 / 19,509 行，`scripts/` 36 个脚本，`web/` 7 个前端文件。

### 1.2 技术栈速览

| 技术 | 用途 | 版本 / 说明 |
|------|------|-------------|
| LangGraph | 状态图引擎 | 1.2，StateGraph / Command / subgraph / checkpointer |
| LangChain | LLM 与工具链 | ChatOpenAI 接入 DeepSeek（OpenAI 兼容） |
| FastMCP | MCP 协议 | 1.30，memory / stdio / direct 三传输 |
| FastAPI + Uvicorn | Web 框架 | REST + SSE 流式响应 |
| SQLAlchemy | ORM | SQLite（本地）/ MySQL 8.0（部署） |
| Redis / fakeredis | 缓存、队列、锁 | TTL 抖动缓存、Stream 消费组、分布式锁 |
| BM25 + 哈希向量 + RRF | 检索栈 | 可选 Qdrant 与腾讯云 TokenHub Embedding |
| pytest / ruff / coverage | 质量保障 | 1055 条离线用例，覆盖率 89% |

### 1.3 完整目录树

```text
src/app/                             # Agent 服务（FastAPI :8000）
├── main.py                          # 应用入口：lifespan 初始化、安全响应头、页面路由、静态挂载
├── config.py                        # Settings（环境变量与全部默认值）
├── auth.py                          # HMAC Token、撤销表、登录限流、部门作用域
├── llm.py                           # LLMClient：对话 / JSON 模式 / 成本估算 / 备用模型 / 前缀缓存
├── resilience.py                    # 重试退避、熔断器 Breaker
├── scheduler.py                     # APScheduler 定时巡检（默认关闭）
├── static_assets.py                 # 静态资源版本化 + 后端能力自述
├── agents/                          # 编排与对话
│   ├── graph.py                     # 研判主图：supervisor→collector→merge→refine→reasoner→verifier→finalize
│   ├── supervisor.py                # 采集子集决策
│   ├── workers.py                   # erp / market / rag 三个采集 Worker
│   ├── reasoner.py                  # 预案生成（JSON + 降级模板）
│   ├── verifier.py                  # 确定性校验 + 模型复核
│   ├── schemas.py                   # Pydantic 状态与计划模型
│   ├── chat_agent.py                # 对话链路：ReAct 装配、SSE 流式、运行详情
│   ├── tool_guard.py                # 工具循环闸门（失败 / 重复 / 预算）
│   ├── grounding.py                 # 出口溯源闸门
│   ├── answer_eval.py               # 11 维答案判定器
│   ├── trajectory.py                # 轮次指标推导
│   ├── chat_store.py                # 会话 / 消息 / 轮次日志
│   ├── checkpoint.py                # 分析线程 checkpointer 与线程淘汰
│   ├── registry.py                  # Agent 声明式注册表（热加载 / 路由 / 工具组裁剪）
│   ├── prompt_loader.py             # 提示词加载、占位符校验、内容哈希版本
│   ├── query_plan.py                # 查询计划（意图 / 实体 / 时间窗 / plan_id）
│   ├── context.py                   # 工作集与组织范围辅助
│   ├── handoff.py                   # 交接契约
│   ├── registry/*.json              # analyst / diagnostician / operator 三个 manifest
│   ├── prompts/*.md                 # 6 份提示词
│   └── skills/*.md                  # 5 份技能手册
├── tools/                           # 对话工具（27 个）
│   ├── platform_tools.py            # 工具定义、四层装配、参数说明、写工具白名单
│   ├── catalog.py                   # SKU 参考目录与部门映射
│   └── platform_contract.py         # 中台契约版本校验
├── mcp_servers/                     # FastMCP Server（仅做端点映射）
│   ├── _platform.py                 # 中台 HTTP 客户端 + 熔断 + 身份头
│   ├── erp_server.py                # 7 个 ERP 工具
│   ├── pailitao_server.py           # 3 个竞品工具
│   └── order_server.py              # 5 个订单/售后工具
├── mcp_client/                      # MCP 客户端
│   ├── client.py                    # 三传输 + 连接池 + 熔断 + 缓存
│   └── registry.py                  # server → 模块映射
├── rag/                             # 检索栈
│   ├── loader.py / chunker.py / index.py      # 清洗、分块、增量索引与对账
│   ├── bm25.py / vectorizer.py / hybrid.py    # 词法、向量、RRF 融合 + MMR
│   ├── rerank.py / embedding.py / vector_store.py  # 重排、向量化、向量库
│   ├── pipeline.py / skill.py                  # 检索入口与 RAG 技能子图
│   ├── conflict.py / facets.py / synonyms.py   # 口径冲突、部门面、同义词
│   └── eval.py                                 # 检索评测（golden / negatives / conflicts / ablation）
├── memory/                          # 四层记忆
│   ├── tool_contract.py / result_store.py      # L0 结果契约与台账
│   ├── session_memory.py / workspace.py        # L1 会话记忆 / L2 工作区
│   ├── org_memory.py / analysis_merge.py       # L3 组织档案与合并
│   ├── prompt_safety.py / user_profile.py      # 提示词转义 / 用户偏好
│   └── runtime.py / prune.py / merge_stats.py  # 运行时上下文、清理、合并指标
├── observability/                   # 延迟分位、工具可靠性、LLM 回调、trace、OTel
├── security/                        # 部门作用域守卫、滑动窗口限流
├── pipeline/                        # 筛查、快照、指标、巡检执行器
├── cache/                           # Redis 工厂、TTL 缓存、分布式锁、Stream 队列
├── db/                              # 引擎与会话、ORM 模型、仓储、演示数据
└── common/                          # 口径冲突通用工具

src/data_platform/                   # 轻量数据中台（FastAPI :8001）
├── api/                             # 路由、鉴权、配额、审计、Agent 注册、缓存
├── ingest/                          # mock 源 → Airflow 模拟 → PII 脱敏
├── dbt_like/                        # stg → dim/fct → mart 分层转换
├── source_spec.py / mock_sources.py # 模拟数据规格与生成
├── governance.py                    # 指标口径注册与脱敏规则
├── capabilities.py                  # 工具分组与作用域声明的唯一来源
└── contract.py / db.py / config.py  # 契约版本、ORM、配置
```

### 1.4 四层架构

| 层 | 职责 | 关键目录 |
|---|---|---|
| 展示层 | Agent 目录页、演示页、项目简介；面板由后端结构化字段驱动 | `web/` |
| Agent 层 | 对话链路、研判链路、四层记忆、权限守卫、可观测与降级 | `src/app/` |
| MCP 层 | 工具名到中台端点的映射、连接池、熔断、缓存 | `src/app/mcp_servers/`、`src/app/mcp_client/` |
| 数据层 | 模拟源入湖、分层转换、治理口径、REST 输出、身份与审计 | `src/data_platform/` |

### 1.5 一次完整对话的数据流

```text
POST /api/chat/stream
  → chat_routes.chat_stream：鉴权 → 频次/并发闸门 → 会话锁 → 落库用户消息
  → chat_agent.run_chat_stream
      → query_plan.build_query_plan（意图/实体/时间窗/plan_id）
      → registry / resolve_agent 选中 Agent
      → build_system_prompt（L2 基线 + L1 事实卡 + 工作集 + 组织范围）
      → build_chat_agent（工具装配 + post_model_hook 校验 + recursion_limit 兜底）
      → _stream_agent：astream 推送 stage / llm_start / llm_end / tool_start / tool_end / delta / tick
      → grounding.ground_reply（来源 id 与数字溯源，必要时重写一次）
  → _build_run_detail（计划、时间轴、步骤、token 与成本、权限、溯源自检）
  → 落库 chat_message / chat_turn_log → SSE done
```

---

## 第2章 — 基础设施层

### 2.1 `src/app/config.py` — 全局配置（459 行）

`Settings` 基于 pydantic-settings，`extra="ignore"`（未声明变量被静默忽略，因此新增变量必须在类中声明）。所有默认值即「零依赖可启动」：SQLite + fakeredis + 内存向量 + 哈希向量。

| 分组 | 关键字段（默认值） |
|---|---|
| 数据层 | `database_url`（SQLite）、`redis_url`（空=fakeredis）、`vector_store`（memory）、`embedding_backend`（hash） |
| 中台 | `platform_api_url`（:8001）、`platform_internal_token`、`platform_mcp_token`、`platform_agent_id`、`platform_timeout_seconds`（15）、`platform_breaker_threshold`（3） |
| 对话 | `chat_max_concurrency`（4）、`chat_tool_call_budget`（15）、`chat_turn_timeout_seconds`（300）、`chat_rate_limit_per_minute`（20） |
| 记忆 | `memory_max_inline_rows`（50）、`memory_context_chars`（3000）、`kb_evidence_max_chars`（6000） |
| 研判 | `inspect_timeout_seconds`（600）、`inspect_collect_timeout_seconds`（45）、`checkpoint_max_threads`（200）、`checkpoint_prune_min_age_seconds`（600）；校验回流上限 `MAX_REVISIONS = 2` 定义在 `agents/graph.py` |
| 检索 | `rag_rrf_k`（60）、`rag_rerank_backend`（heuristic）、`rag_rerank_timeout_ms`（400）、`rag_max_per_doc`（2） |
| 缓存 | `cache_ttl_platform`（120）、`cache_ttl_mcp`（600）、`cache_ttl_rag`（600）、`cache_jitter_ratio`（0.2） |
| 治理 | `write_tools_roles`（`["admin"]`）、`agents_enabled`（true）、`metric_version`（2026Q1）、`security_record_window_hours`（4） |

### 2.2 `src/app/db/` — 引擎、模型与演示数据（235 / 580 / 326 / 497 行）

- `engine.py`：`make_engine()` 对 SQLite 注入 `journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`；`session_scope()` 是统一事务边界，提交成功后才执行 `session.info["after_commit"]` 登记的回调（例如把档案写进 Markdown 文件）；`ensure_columns()` 对老库做幂等补列与索引升级。
- `models.py`：SKU、小时流水、异常、研判运行、审批、售后、KB 文档与分块、分析档案、工具结果台账、会话与轮次日志、鉴权事件与撤销令牌等 ORM 表。
- `repos.py`：仓储层查询（指标聚合、异常列表、轮次日志等）。
- `seed.py`：演示数据生成（300 SKU = 20 款 × 3 色 × 5 码，30 天小时流水，注入 7 类异常共 46 个 SKU）。

### 2.3 `src/app/cache/` — 缓存、锁与队列（119 / 236 / 193 / 256 行）

| 文件 | 要点 |
|---|---|
| `redis_factory.py` | 同步与异步客户端各一份；`REDIS_URL` 为空时自动回落 fakeredis，保证离线可运行 |
| `ttl_cache.py` | TTL 随机抖动防雪崩；单飞（single-flight）合并同 key 并发回源；回源者被取消时向等待者下发可重试信号而非 `CancelledError`；Redis 异常降级为 miss |
| `lock.py` | `redis_lock`：`SET NX PX` + token 比对释放；`async_short_lock` / `short_lock`：进程内锁叠加 Redis 锁；会话锁 TTL 由「最坏单轮 + 30s」派生 |
| `stream_queue.py` | 幂等入队（`SET NX EX` + `XADD`）、消费组读取、`XAUTOCLAIM` 回收未确认消息、投递计数超限进死信、stats 与死信明细 |

### 2.4 `src/app/resilience.py`、`auth.py`、`llm.py`（125 / 331 / 351 行）

- `resilience.py`：指数退避 + 抖动、`Retry-After` 优先、三态熔断器（closed / open / half-open，半开只放一个探测）。
- `auth.py`：HMAC 签名 Token（默认 8 小时）、`revoked_token` 落库（重启仍失效）、登录限流（账号 + IP 维度）、`scope_categories()` 决定部门账号的可见品类。
- `llm.py`：`LLMClient` 封装对话调用与 JSON 模式调用（Pydantic 校验失败自动修复重试一次）、Token 用量与成本估算、主模型额度耗尽时切换备用模型、返回 `prompt_cache` 命中信息。

---

## 第3章 — MCP 网关层

### 3.1 `src/app/mcp_servers/_platform.py` — 中台 HTTP 客户端（163 行）

所有 MCP 工具最终都请求数据中台 REST：`_token()` 优先使用 `PLATFORM_MCP_TOKEN`（未配置时复用 `PLATFORM_INTERNAL_TOKEN`）；请求头携带 `X-Agent-Id: mcp-<server>` 与 `X-Trace-Id`，审计侧按声明式身份归因；5xx 与连接失败抛 `McpPlatformError`（计入熔断），4xx 作为业务错误返回。

### 3.2 三个 FastMCP Server（153 / 56 / 98 行）

| Server | 工具 | 说明 |
|---|---|---|
| `erp_server.py` | 7 个：库存、销量序列、在途、调拨、同款尺码、供应商、大促活动 | 覆盖研判所需事实数据 |
| `pailitao_server.py` | 3 个：同款竞品、价格带、热度 | 竞品行情按需采集 |
| `order_server.py` | 5 个：订单状态、物流、下单、售后建单、售后查询 | 写操作走人工确认 |

Server 不直连数据库，只做「工具名 → 中台端点」映射，因此中台可独立部署与替换。

### 3.3 `src/app/mcp_client/client.py` — 客户端与连接池（445 行）

- 三传输：`memory`（进程内直连）、`stdio`（子进程）、`direct`（HTTP SSE）。
- `_PooledClient`：每个 Server 默认 2 条会话，调用投递给「排队最少」的一条，解决同类工具互相阻塞。
- `_Breaker`：按 Server 计连续失败 3 次后打开 60 秒；窗口内快速失败（`outcome=breaker_open`），结束后半开放一个探测。
- 未知工具名或未注册 Server 直接快速失败（`retryable=False`），不重试、不计熔断失败。
- 工具结果可走 TTL 旁路缓存（`use_cache`），业务错误不缓存。

---

## 第4章 — 工具层

### 4.1 `src/app/tools/platform_tools.py` — 27 个工具与四层装配（1177 行）

**工具分组**（`TOOL_GROUPS`，共 9 组 27 个）：`read` 10、`style` 4、`write` 2、`order` 3、`memory` 3、`skill` 2、`approval` 1、`kb` 1、`meta` 1；`RESERVED_GROUPS` 预留 `image` 组（声明即报错，避免空集被当作「未声明」而回退全量）。

**装配顺序（由内到外）**：

```text
scope_guard（部门作用域）→ 写权限拒绝桩（WRITE_TOOL_NAMES × WRITE_TOOLS_ROLES）
  → with_result_contract（result_id / as_of / summary / truncated / artifact_path）
  → with_tool_loop_guard（失败 / 重复 / 预算三层闸门，最外层）
```

**关键机制**：

| 机制 | 实现 |
|---|---|
| 参数说明 | `PARAM_DOCS` + `_tool_args_schema()` 为每个参数生成 `Field(description=...)`，模型可见描述与代码同源 |
| 部门作用域 | `TOOL_SCOPES` 声明 + `security/scope.py` 校验；新增工具未声明作用域时装配期直接失败 |
| 写工具控制 | `WRITE_TOOL_NAMES = {create_replenishment_order, create_after_sales, save_analysis}`，默认仅 `WRITE_TOOLS_ROLES` 内角色可调 |
| 幂等写 | `create_replenishment_order` 以「会话 + 轮次 + SKU + 数量 + 原因」生成幂等键，模型无法伪造 |
| 档案归属 | `save_analysis` 依据本轮取数证据推断部门，非管理员被夹到本人可写部门，返回 `department_source` |
| 能力目录 | 分组与作用域以中台 `capabilities` 为唯一来源，Agent 侧用离线快照，`/api/health.capabilities` 标注漂移 |

### 4.2 `src/app/tools/catalog.py`、`platform_contract.py`（196 / 73 行）

`catalog.py` 维护 SKU 参考目录（部门映射），中台目录不可用时可回落到本地表；`platform_contract.py` 校验响应 `schema_version`，主版本不符返回 `error_kind=contract`（不重试、不计熔断）。

### 4.3 `src/app/security/`（174 / 112 行）

`scope.py` 提供 `ensure_sku_visible` / `ensure_order_visible` / `ensure_style_visible` / `filter_visible_items` 与 `SCOPE_KINDS`；`throttle.py` 是滑动窗口限流底座（登录与对话限流共用）。

---

## 第5章 — Agent 编排层

### 5.1 `src/app/agents/graph.py` — 研判主图（245 行）

节点与路由：`supervisor` → `collector` → `merge` → `reasoner` → `verifier` →（不通过且 `attempts < MAX_REVISIONS`）→ `refine` → `reasoner`；通过或达上限 → `finalize`。`degraded` 在状态中标注为 `Annotated[bool, operator.or_]`（粘性，后续节点无法改回 False）。`build_inspection_graph()` 负责编译带 checkpointer 的图。

### 5.2 `supervisor.py` / `workers.py`（78 / 160 行）

`supervisor_node` 决定采集子集（ERP 必选，竞品/知识库可选）并生成简短任务简报；`erp_worker` / `market_worker` / `rag_worker` 各自独立超时与异常隔离（`gather(return_exceptions=True)`），单源失败只影响该源，整轮标记 `degraded` 继续。

### 5.3 `reasoner.py` / `verifier.py` / `schemas.py`（122 / 160 / 70 行）

`reasoner_node` 输出 JSON 预案（Pydantic 校验失败走 `_fallback_plan` 确定性模板并标 `degraded`）；`verifier_node` 先跑 `_det_checks`（引用有效性、数字与证据一致性、清仓折扣下限与分批比例、补货阈值与单次上限、SOP 顺序），再交模型复核，结论写入 `verification`。

### 5.4 `src/app/skills/` — Skill 子图（40 / 138 / 99 行）

| 文件 | 口径 |
|---|---|
| `stock.py` | `available_of()`：可用库存优先（`available` 允许为 0，缺字段才回落 `stock`） |
| `inventory_judge.py` | 断码口径（本码为主销码且可用为 0）、可售天数、健康度、风险标签、促销窗口判断 |
| `replenishment.py` | `safety = ceil(daily × lead × 0.3)`、`reorder_point = ceil(daily × (lead + 3)) + safety`、`target_days = lead + 21`、MOQ 取整、单次上限 `MAX_SINGLE_REPLENISH = 500` |

### 5.5 `registry.py` / `prompt_loader.py` / `query_plan.py`（378 / 99 / 355 行）

- `registry.py`：加载 `registry/*.json`，按目录与文件 mtime 热加载；`route()` 实现「显式指定 > 关键词 > `query_plan.intent` > 默认」；未声明 `write` 组的 Agent 会被剔除写工具并记入 `stripped_write_tools`；`AGENTS_ENABLED=false` 时只返回默认 Agent（`routed_by="agents_disabled"`）。
- `prompt_loader.py`：读取 `prompts/*.md`、校验 `{placeholder}` 白名单、按归一化文本生成内容哈希版本号（CRLF/LF 不影响版本）。
- `query_plan.py`：解析意图、实体（SKU/款号/尺码/部门）、时间窗、所需数据与假设，`plan_id = sha1(规范化 JSON)`，同输入跨会话稳定。

---

## 第6章 — 对话链路

### 6.1 `chat_agent.py` — ReAct 装配与运行详情（1286 行）

| 关键函数 | 职责 |
|---|---|
| `build_system_prompt` | 组装系统提示：L2 基线（`AGENTS.md`）→ 治理口径 → 人工确认阈值 → 会话记忆 / 工作集 / 组织范围 → 今天与账号（静态在前、动态在后） |
| `build_chat_agent` | 装配 `create_react_agent`：模型、工具、`post_model_hook=validate_tool_calls(...)`、`recursion_limit` |
| `_recursion_limit_for` | `max(8, 3 × 预算 + 6)`：每次工具循环 3 个超步（agent → hook → tools），再加 6 步兜底 |
| `resolve_agent` | Agent 选择（含 `agents_enabled` 总开关与不可用回落） |
| `_stream_agent` | `astream(stream_mode=["messages","values"])`，过滤工具调用片段与工具消息，推送 `delta` |
| `_invoke_agent` | 非流式调用；模型名幻觉时做一次纠错重试，返回 6 元组（消息、增量、重试次数、使用模型、是否备用、纠错次数） |
| `_build_run_detail` / `_build_timeline` | 组装运行详情：计划、时间轴（阶段 / 模型 / 工具）、Token 与成本、权限、溯源自检 |
| `_run_chat_body` | 主流程：记忆装载 → 计划 → Agent → 工具循环 → 溯源 → 落库 |

### 6.2 `tool_guard.py` — 工具循环闸门（341 行）

按轮次维护 contextvar 守护状态：同一工具 + 同入参指纹连续成功 3 次（`REPEAT_LIMIT`）、连续失败 3 次（`FAILURE_LIMIT`，仅链路类失败计入）、成功调用数超预算三者任一命中即抛 `ToolLoopError` 并中止本轮。`validate_tool_calls()` 作为 `post_model_hook` 校验模型给出的工具名与入参，非法时抛带错误码的异常；`guard_summary()` 供 `run_detail.guard` 展示。

### 6.3 `grounding.py` — 出口溯源闸门（252 行）

`check_reply()` 抽取来源 id（`res_xxx` / `doc#chunk`）与带单位数字，校验来源真实存在于本轮工具结果；本轮调过工具却无任何来源、或 ≥3 个数字全部无法溯源时判失败。`ground_reply()` 失败后让模型重写一次（只改数字与来源），仍不达标则保留答案并标 `degraded`、附带告警文案。

### 6.4 `answer_eval.py` / `trajectory.py`（318 / 185 行）

`answer_eval.judge()` 以 11 个维度判定单轮答案（工具、入参、引用、溯源、写操作、禁用工具、必提项、禁提项、答案质量、会话引用、澄清）；`trajectory.py` 从 `chat_turn_log.payload` 纯函数推导每轮工具数、步数、重复调用、预算利用率与多轮成功率。

### 6.5 `chat_store.py` / `checkpoint.py`（293 / 242 行）

`chat_store` 负责会话、消息与轮次日志（含保留条数与 24 小时清理）；`checkpoint` 提供 `AsyncSqliteSaver` / Redis saver 切换、`touch_thread()` 与按 `CHECKPOINT_MAX_THREADS` 淘汰最旧线程（附最小保护窗口）。

---

## 第7章 — 记忆与提示词

### 7.1 L0：`tool_contract.py` / `result_store.py`（130 / 197 行）

`with_result_contract()` 装饰器为每个工具返回补齐 `result_id` / `as_of` / `summary` / `truncated` / `artifact_path` 并写 `tool_result` 台账；单次结果超过 `MEMORY_MAX_INLINE_ROWS` 行时只回前 3 行 + 行数 + artifact 路径，完整明细落 `data/artifacts/<session>/<result_id>.jsonl`。

### 7.2 L1：`session_memory.py` / `workspace.py` / `user_profile.py`（328 / 88 / 78 行）

`session_memory` 维护事实卡（结论骨架：结论句 + 数值 + `result_id`）与工作集实体表（SKU / 款号 / 尺码 / 订单），提供 `build_facts_card()`、`merge_working_set()`、`should_ask_clarify()`（同缺口只追问一次）；`workspace.py` 注入 `AGENTS.md` 基线与 Skills 索引；`user_profile.py` 持久化用户偏好。

### 7.3 L3：`org_memory.py` / `analysis_merge.py` / `merge_stats.py`（740 / 513 / 115 行）

`org_memory` 把结论沉淀为「DB 行 + Markdown 档案」双写：`save_analysis()` 按内容哈希去重、按证据推断部门、登记 `after_commit` 回调写文件；`search_analyses()` 支持部门过滤与关键词检索；`resolve_session_reference()` 解析 `@session` 引用（id 形态严格查找，三态 `ok / denied / not_found`）；`analysis_merge` 做档案主文档合并（fire-and-forget + `_pending` 队列 + 脚本补做），`merge_stats` 输出积压与 SLO 判定。

### 7.4 `prompt_safety.py`（35 行）

`escape_tag_text()` 与 `safe_ref_token()`：所有进入提示词区块的用户可控文本统一转义与截断，防止闭合标签、伪造状态与上下文撑爆。

### 7.5 提示词与技能（6 + 5 份）

| 文件 | 用途 |
|---|---|
| `prompts/chat_system.md` | 对话 Agent 主提示词（口径、取数纪律、工具使用、输出风格） |
| `prompts/supervisor.md` / `reasoner.md` / `verifier.md` | 研判链路三个节点的角色提示 |
| `prompts/diagnostician.md` / `operator.md` | 诊断 Agent（只读）与操作 Agent（写操作走人工确认） |
| `skills/restock-calc.md` / `size-gap-diagnosis.md` / `overstock-review.md` / `sku-screening.md` / `weekly-ops-review.md` | 模型按需 `load_skill` 加载的操作手册，口径与代码对齐（由 `eval_agent_tooling` 第 6 组校验） |

---

## 第8章 — 检索栈

### 8.1 分块与索引：`loader.py` / `chunker.py` / `index.py`（349 / 125 / 350 行）

`loader` 解析 front matter（部门/品类/季节/生效期/版本/`key_facts`）；`chunker` 按标题切段 + 320 字滑窗 / 60 重叠，`chunk_id = doc_id#sha1(标题+正文)[:8]`（内容寻址，文档改动只影响变动块）；`index` 按 `sha256 + chunker_version` 增量重建，块 id 差集同步 SQLite 与向量库（写库前 `delete_doc` 清理陈旧块），并按文档指纹对账。

### 8.2 召回：`bm25.py` / `vectorizer.py` / `hybrid.py`（145 / 77 / 438 行）

`bm25` 为字段加权的倒排索引（标题 2.0 / 小节 1.5 / 正文 1.0），只对命中词打分；`vectorizer` 生成 512 维字符 n-gram 哈希向量；`hybrid` 做双路召回 + **RRF 融合（k=60）**、同文档配额与 MMR 去重，并在召回前做查询改写（`rewrite_query`）。

### 8.3 重排与向量化：`rerank.py` / `embedding.py` / `vector_store.py`（225 / 303 / 304 行）

`rerank` 支持 `none / heuristic / llm / cross` 四后端，带超时预算，任何失败回退融合序并打 `rerank_degraded`；`embedding` 支持腾讯云 TokenHub（1024 维，429 读 `Retry-After`，401/403 不重试）；`vector_store` 支持内存余弦与 Qdrant（集合按维度命名，payload 只存元数据）。

### 8.4 检索入口与口径冲突：`pipeline.py` / `conflict.py` / `facets.py` / `synonyms.py`（256 / 438 / 82 / 75 行）

`pipeline.search_kb()` 带 TTL 缓存返回 `citations` / `conflicts` / `conflict_check` / `scope_notice` / `uncovered_departments`；`conflict.detect()` 在候选池上按权威度（中台治理口径 > 部门细则 > 通用规则 > 历史案例）裁决，输出 `is_primary` 与 `## 冲突提示`；`facets` 识别问句部门面（童装归女装）并作为可比性闸门；`synonyms` 做查询扩展。

### 8.5 评测：`eval.py`（340 行）

支持 `golden`（38 题 Recall@K / MRR / nDCG）、`negatives`（20 题弃答率）、`conflicts`（25 题权威度裁决）、`ablation`（bm25 / vector / rrf × rerank 对照）。

---

## 第9章 — 数据中台

### 9.1 数据生成与入湖：`source_spec.py` / `mock_sources.py` / `ingest/`（249 / 324 / 各 27–165 行）

`source_spec` 定义模拟数据规格（SKU、流水、订单、物流、售后、CRM 预订、活动）与 7 类异常注入；`mock_sources` 生成快照；`ingest/` 用 `airflow_mock.py` 模拟 DAG 调度与重试，`sync_*.py` 完成入湖，`common.py` 做 PII 脱敏（姓名、手机、收货人）。

### 9.2 分层转换：`dbt_like/models.py` / `run_transforms.py`（400 / 49 行）

stg（清洗）→ dim（维度）→ fct（事实）→ mart（指标），其中 mart 提供款级指标：尺码曲线与 `main_size_flag`（占比 ≥ `MAIN_SIZE_SHARE = 0.15`）、断码标记、齐码率、周转天数、售罄率、生命周期阶段。

### 9.3 治理与接口：`governance.py` / `capabilities.py` / `api/`（217 / 167 / 1410+263+82+68+242+436+111 行）

`governance` 注册指标口径、脱敏规则与 `prompt` 上下文；`capabilities` 是工具分组与作用域声明的唯一来源；`api/main.py` 组装应用与中间件，`authz.py` 实现 Key → `agent_id` → scope / departments，`quota.py` 按调用方限流，`audit.py` 写 `api_audit_log`，`agents.py` 支持运行时注册（令牌只返回一次、存 sha256），`routes.py` 提供业务端点（含审批、售后、调拨），`schemas.py` 定义请求/响应模型，`cache.py` 为读接口提供 TTL 抖动缓存。

---

## 第10章 — Web API 与前端

### 10.1 `src/app/api/routes.py`（731 行）

| 分区 | 端点 |
|---|---|
| 健康与指标 | `/api/health`（19 个子块）、`/api/metrics`、`/api/metrics/agents`、`/api/metrics/trajectory` |
| 研判 | `POST /api/inspect`、`GET /api/inspections/{run_id}`、`POST /api/inspect/followup` |
| 巡检 | `POST /api/patrol`、`GET /api/patrol/{run_id}`、`POST /api/patrol/{run_id}/cancel`、`GET /api/patrol/recent` |
| 检索与查询 | `/api/rag/search`、`/api/skus`、`/api/anomalies`、`/api/queue/stats`、`/api/cache/breakdown` |
| 认证 | `/api/auth/login`、`/me`、`/logout`、`/security-records` |
| 定时任务 | `/api/scheduler/status`、`/api/scheduler/run-once` |

### 10.2 `src/app/api/chat_routes.py`（986 行）

对话入口与人工确认：`chat` / `chat_stream` 之前完成鉴权、频次与并发闸门、会话锁（`_SessionGuard`，冲突在建流前返回 409）、用户消息落库；`_execute_turn` 负责运行 Agent、持久化回复与轮次日志、失败轮次留痕。SSE 事件协议：`stage` / `llm_start` / `llm_end` / `tool_start` / `tool_end` / `delta` / `tick` / `loop_aborted` / `error`（`fatal=true`）/ `done`。审批与售后：`/api/actions/pending`、`approve`、`reject`、`/api/actions/after-sales*`。

### 10.3 `src/app/static_assets.py` 与 `web/`

`static_assets.render_page_html()` 为页面注入资源内容哈希（`?v=`），`app_features()` 提供后端能力自述供前端自检。前端为原生 JS 无构建：`web/index.html`（演示页）+ `web/app.js`（2897 行：SSE 渲染、运行详情、记忆面板、历史会话）、`web/agents.html` + `web/agents.js`（目录页，数据驱动）、`web/style.css`、`web/about.html` + `web/markdown-lite.js`。详见 `web/README.md`。

---

## 第11章 — 可观测、运维与质量保障

### 11.1 `src/app/observability/`（105 / 168 / 189 / 118 / 120 / 35 行）

`latency` 分类采样并输出 P50/P95/P99；`tool_stats` 按 `server.tool` 记录成功率、重试、超时与失败分类；`llm_callback` 捕获每次模型调用的事件与用量；`trace` 生成并注入 `trace_id`；`otel` 可选导出 span；`runtime_state` 暴露进程内状态（进行中对话数、队列等）。

### 11.2 `src/app/pipeline/`（286 / 76 / 231 / 601 / 77 行）

`screening` 用量化特征与阈值做异常初筛；`snapshot` 生成小时快照；`metrics` 聚合看板指标；`patrol` 是批量巡检执行器（筛查 → 幂等入队 → 并发消费 → 回收未确认消息 → 死信 → 汇总简报，支持协作终止）；`progress` 输出进度事件。

### 11.3 `src/app/main.py` 与 `scheduler.py`（258 / 131 行）

`lifespan` 内完成建表与补列、知识库增量索引、能力目录同步、MCP 启动与关闭、可选定时任务；中间件下发安全响应头（nosniff / DENY / CSP）与静态资源 no-cache；`/`、`/agents.html` 走服务端渲染注入版本号，`/about.md` 提供项目简介源文本，其余静态资源挂载在根路径。

### 11.4 脚本、测试与 CI

| 类别 | 内容 |
|---|---|
| 初始化 | `init_db.py`、`platform_bootstrap.py`、`ensure_init.py`、`demo_*.py` |
| 评测（11 个） | `eval_accuracy` / `eval_agent_answers` / `eval_agent_tooling` / `eval_apparel` / `eval_degradation` / `eval_grounding` / `eval_permissions` / `eval_platform_governance` / `eval_rag` / `eval_routing` / `eval_trajectory` |
| 基准（4 个） | `bench_chat` / `bench_mcp` / `bench_multi_agent` / `bench_rag_scale` |
| 校验与运维 | `verify_stack` / `verify_concurrent_writes` / `verify_three_turns` / `ui_smoke.js` / `dedupe_analyses` / `merge_analyses` / `render_analysis_md` |
| 测试 | `tests/` 114 个文件、19,509 行，全离线（FakeLLM / MockTransport / fakeredis / qdrant local） |
| CI | `.github/workflows/ci.yml`：12 道门禁 + MySQL service 分支 |

---

## 附录 A — 一次研判请求的执行流程

```text
POST /api/inspect {sku_id}
  → routes.inspect：建 run 记录 → patrol.inspect_sku（分布式锁 + 硬超时）
  → graph.build_inspection_graph（带 checkpointer，thread_id = run_id）
      supervisor → 决定 tasks（erp 必选）
      collector → erp / market / rag worker 并发（单源独立超时 + 异常隔离）
      merge     → inventory_judge / replenishment 两个 Skill 子图并行
      reasoner  → 结构化预案（失败走确定性模板 + degraded）
      verifier  → 确定性校验 + 模型复核 → 通过 / 带问题回流（≤2 轮）
      finalize  → 汇总结论、证据、动作、校验清单与轨迹
  → repos 落库异常与预案结果 → 高风险补货进入「人工确认」待办
  → GET /api/inspections/{run_id} 读取结果与 trace
```

## 附录 B — 关键常量速查

| 常量 | 值 | 位置 |
|---|---|---|
| 单次补货上限 | 500 件 | `skills/replenishment.py::MAX_SINGLE_REPLENISH` |
| 人工确认阈值 | ≥200 件 或 ≥5 万元 | `platform_tools.py` 工具说明 + 中台审批 |
| 安全库存 | `ceil(日均 × 交期 × 0.3)` | `skills/replenishment.py` |
| 再订货点 | `ceil(日均 × (交期 + 3)) + 安全库存` | `skills/replenishment.py` |
| 主销码占比 | ≥ 15% | `data_platform/dbt_like/models.py::MAIN_SIZE_SHARE` |
| 分块窗口 | 320 字 / 60 重叠 | `rag/chunker.py` |
| RRF 常数 | k = 60 | `config.py::rag_rrf_k` |
| 工具预算 | 15 次 / 轮 | `config.py::chat_tool_call_budget` |
| 循环闸门 | 失败 3 / 重复 3 / 预算 | `agents/tool_guard.py` |
| 递归上限 | `3 × 预算 + 6` | `agents/chat_agent.py::_recursion_limit_for` |
| 内联明细上限 | 50 行 | `config.py::memory_max_inline_rows` |
| 熔断 | 3 次 / 60 秒 | `config.py` + `resilience.py` |

## 附录 C — 模块与测试对照

| 模块 | 主要测试 |
|---|---|
| 研判主图 | `test_graph_routing.py`、`test_graph_mock.py`、`test_graph_degradation.py` |
| 对话与流式 | `test_react_loop_offline.py`、`test_react_loop_stream_offline.py`、`test_chat_stream_tokens.py`、`test_chat_stream_status.py` |
| 工具闸门与溯源 | `test_tool_guard.py`、`test_grounding.py`、`test_unknown_tool_recovery.py` |
| 工具面与作用域 | `test_agent_tool_surface.py`、`test_tool_scope.py`、`test_permissions`（脚本） |
| 记忆四层 | `test_memory_layers.py`、`test_session_reference.py`、`test_org_memory_scope.py`、`test_analysis_concurrency.py` |
| 检索栈 | `test_rag.py`、`test_rag_fusion.py`、`test_rag_rerank.py`、`test_rag_conflict.py`、`test_rag_backend.py` |
| 中台与治理 | `test_platform_governance.py`、`test_platform_authz.py`、`test_platform_contract.py`、`test_agent_registration.py` |
| 前端与静态 | `test_ui_pages.py`、`test_ui_adjustments.py`、`test_stream_latency.py`、`ui_smoke.js` |
| 并发与幂等 | `test_concurrency_limits.py`、`test_tool_idempotency.py`、`verify_concurrent_writes.py` |
