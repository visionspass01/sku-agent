# 配置（.env）

> 所有可调参数集中在 `src/app/config.py`（业务筛选阈值在 `ScreeningThresholds`）与 `src/data_platform/config.py`。
> 复制 `.env.example` 为 `.env` 后按需修改；**默认值即可零依赖启动**（SQLite + fakeredis + 内存向量 + 哈希 embedding）。

## 1. 必填 / 基础

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 必填 | DeepSeek 官方 Key（对话与研判都走它） |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 兼容 OpenAI 协议 |
| `LLM_MODEL` | `deepseek-flash` | 也可 `deepseek-v4-pro`（更慢更强） |
| `LLM_DISABLE_THINKING` | `true` | 关闭思维链：单次调用 15s → 8s 量级 |
| `LLM_PRICE_INPUT` / `LLM_PRICE_OUTPUT` | `1` / `4` | 成本估算单价（元/百万 tokens），只作量级参考 |
| `AUTH_SECRET` | `change-me-demo-secret` | Token 签名密钥（上线必须替换） |
| `AUTH_TOKEN_TTL_HOURS` | `8` | 登录 Token 有效期（登出写入持久化黑名单，重启仍失效） |
| `LOGIN_RATE_LIMIT` / `LOGIN_RATE_WINDOW_SECONDS` | `5` / `60` | 同一「账号+IP」窗口内失败上限，超过返回 429 + `Retry-After` |

## 2. 存储与缓存

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | 本地 SQLite `data/sku_demo.db`；**compose 内默认 MySQL** | 显式覆盖成 `mysql+pymysql://user:pass@host:3306/sku_demo?charset=utf8mb4` 或 `sqlite:///...` |
| `PLATFORM_DB_URL` | 本地 SQLite `data/platform/platform.db`；compose 内默认 MySQL `platform` 库 | 中台库（与 Agent 库分开） |
| `CHECKPOINT_BACKEND` / `CHECKPOINT_PATH` | `sqlite` / 空（`data/checkpoints.sqlite`） | 研判线程检查点：sqlite（默认，重启可续）/ redis（多副本，需 `langgraph-checkpoint-redis`） |
| `CHECKPOINT_MAX_THREADS` | `200` | 线程数上限，超过按最近活跃时间淘汰最旧（`/api/health.checkpoint` 可看当前用量） |
| `CHECKPOINT_PRUNE_MIN_AGE_SECONDS` | `600` | 淘汰保护窗口：最近这么多秒内被 touch 过的线程不参与淘汰（研判/追问开始时也会 touch）。防止"还在跑的长研判被并发裁剪删掉，追问复用不到上一轮采集数据" |
| `REDIS_URL` | 空（fakeredis） | 配真实 Redis 地址即切换（TTL 缓存 / Stream / 锁） |
| `REDIS_SOCKET_TIMEOUT` / `REDIS_SOCKET_CONNECT_TIMEOUT` | `3` / `3` | 读/连接超时（秒）。**读超时必须设**：redis-py 默认 `None`=无限等，而缓存/队列的读都在事件循环里 |
| `REDIS_HEALTH_CHECK_INTERVAL` | `30` | 空闲连接的存活探测间隔（秒），避免拿到已被对端关闭的连接 |
| `QUEUE_MIN_IDLE_MS` | `60000` | PEL 里的消息闲置多久可被 `XAUTOCLAIM` 回收（重投） |
| `QUEUE_MAX_DELIVERIES` | `3` | 同一条消息最多投递几次，超过进死信（`queue:dead`），不再无限重投 |
| `QUEUE_CLAIM_BATCH` | `20` | 单轮巡检最多回收多少条遗留消息 |
| `CACHE_TTL_MCP` / `CACHE_TTL_RAG` / `CACHE_TTL_PLATFORM` | `600` / `600` / `120` | 各类缓存 TTL（带随机抖动 + 单飞，防雪崩/防击穿） |
| `CACHE_TTL_NEGATIVE` | `10` | 负缓存 TTL（秒）：中台**连不上**时把这次失败短暂缓存，避免每轮都等满 `PLATFORM_TIMEOUT_SECONDS` |
| `PLATFORM_BREAKER_THRESHOLD` / `PLATFORM_BREAKER_RESET_SECONDS` | `3` / `60` | 中台熔断：连续链路失败几次打开、保持多久（半开只放一个探测，`/api/health.platform.breaker` 可看） |

## 3. 检索栈（可选）

| 变量 | 默认 | 说明 |
|---|---|---|
| `VECTOR_STORE` | `memory` | `memory`（进程内 numpy）/ `qdrant`（外部向量库） |
| `QDRANT_URL` | 空 | `http://qdrant:6333`；本地无 Docker 用 `local` 或 `local:data/qdrant_local` |
| `QDRANT_COLLECTION` | `kb_chunks` | 实际集合名带维度后缀（如 `kb_chunks_1024`） |
| `EMBEDDING_BACKEND` | `hash` | `hash`（本地 512 维，零依赖）/ `tencent`（TokenHub） |
| `TENCENT_EMBEDDING_API_KEY` | 空 | TokenHub 控制台创建 |
| `TENCENT_EMBEDDING_BASE_URL` | `https://tokenhub.tencentmaas.com/v1` | 新加坡用 `tokenhub-intl.tencentmaas.com` |
| `TENCENT_EMBEDDING_MODEL` | `kinfra-text-embedding-0.6b` | 0.6b=1024 维 / 4b=2560 维 |
| `EMBEDDING_TIMEOUT_SECONDS` / `EMBEDDING_MAX_RETRIES` / `EMBEDDING_BATCH_SIZE` | `8` / `2` / `64` | 超时、重试、单批条数（≤128） |
| `RAG_RRF_K` | `60` | RRF 融合常数：`score = Σ 1/(k+rank)` |
| `RAG_MAX_PER_DOC` / `RAG_MMR_LAMBDA` | `2` / `0.7` | 同一文档最多占 top-k 几条 / MMR 相关度权重（1.0=纯相关度） |
| `RAG_MIN_VECTOR_SCORE` / `RAG_MIN_COVERAGE` / `RAG_MIN_KEYWORD_SCORE` | `0.25` / `0.25` / `15` | **无答案判定**阈值（向量余弦 / 词覆盖 / 词法证据反制），见 [RAG.md](RAG.md#22-无答案rrf-回答不了有没有答案) |
| `RAG_RERANK_BACKEND` | `heuristic` | `none`/`heuristic`/`llm`（DeepSeek listwise）/`cross`（BGE-reranker，需 `pip install -e .[rerank]`） |
| `RAG_RERANK_MAX_CANDIDATES` / `RAG_RERANK_TIMEOUT_MS` | `30` / `400` | 候选上限与重排预算（超时回退融合序并标 `rerank_degraded`） |
| `RAG_RERANK_MODEL` | `BAAI/bge-reranker-base` | `cross` 后端的模型名 |
| `RAG_INDEX_RECONCILE` / `RAG_INDEX_WARMUP` | `true` / `true` | 启动时按文档指纹对账向量库 / 启动预热检索器 |
| （知识库部门面，无环境变量） | 词表在 `src/app/rag/facets.py::DEPARTMENT_WORDS`；跨部门比较意图词表在 `DEPARTMENT_COMPARISON_WORDS`（只收**部门语义**词：哪个部门/各部门/跨部门/部门之间/各线；“区别/对比/分别/相比”这类通用比较词不算） | 问句里识别 男装/女装/童装（童装归女装）→ 管理员侧把检索收敛到「该部门 + 通用」，并作为口径裁决的可比性闸门；**识别失败按"无部门面"处理**（通用口径 + 部门例外），不会给出错误部门的裁决。问句部门面与账号可见面不一致时只按通用口径作答并给 `scope_notice`；多部门 / 跨部门比较不做单一裁决，未声明口径的部门写"按通用执行"（`uncovered_departments`，仅当该部门文档确实被召回时才列） |
| `KB_EVIDENCE_MAX_CHARS` | `6000` | 知识库结果进溯源校验时保留的字符数（普通详单工具仍截 500 字），见 [RAG.md](RAG.md#25-知识库回答也要能溯源否则出口闸门会误杀) |

## 4. 数据与 MCP

| 变量 | 默认 | 说明 |
|---|---|---|
| `SEED` / `SEED_DAYS` | `42` / `30` | 模拟数据规模（300 SKU × 30 天小时流水） |
| `MCP_TRANSPORT` | `memory` | `memory` / `stdio` / `direct`（测试） |
| `MCP_CALL_TIMEOUT` / `MCP_MAX_RETRIES` | `30` / `2` | MCP 调用超时与重试 |
| `MCP_POOL_SIZE` | `2` | 每个 MCP server 的会话数（>1 时同 server 并行取数不排队，见 `scripts/bench_mcp.py`） |
| `MCP_BREAKER_THRESHOLD` / `MCP_BREAKER_RESET_SECONDS` | `3` / `60` | 连续失败几次开熔断、熔断保持时长（期间快速失败，`outcome=breaker_open`）；半开期间**只放一个探测请求** |
| `PLATFORM_API_URL` | `http://127.0.0.1:8001` | 中台地址（Agent 只走 REST） |
| `PLATFORM_INTERNAL_TOKEN` | `demo-internal-token` | 中台内部令牌。**不配 `PLATFORM_API_KEYS` 时它是全权主体**（兼容旧部署） |
| `PLATFORM_MCP_TOKEN` | 空 | MCP 通道专用令牌（可选）：留空则 MCP Server 复用 `PLATFORM_INTERNAL_TOKEN`；配了之后 MCP 走独立令牌——要让中台侧真正分账（独立主体/独立配额），把该 token 也登记进 `PLATFORM_API_KEYS`。审计里的 `mcp-erp`/`mcp-order` 来自请求头 `X-Agent-Id` 的声明式归因，与令牌无关 |
| `PLATFORM_AGENT_ID` | `sku-agent` | Agent 侧调用方身份：中台按 `agent_id` 做授权/配额/审计 |
| `PLATFORM_CONTRACT_STRICT` | `false` | 契约校验严格度：`true` 时响应缺 `schema_version` 也当错误（生产建议开） |
| `PLATFORM_API_KEYS` | 空 | **多 Agent 治理**（JSON）：`{"sku-agent":{"key":"k1","scopes":["read","write","approve"],"departments":null,"rate_limit":600},"bi-agent":{"key":"k2","scopes":["read"],"departments":["女装"]}}`；scope 取值 `read/write/approve/etl/admin`（`admin` 用于运行时注册新 Agent），`departments=null` 表示全部门。**也可以不重启中台**：用带 `admin` 的 key 调 `POST /api/v1/agents` 注册（见 DEPLOY.md §9.2） |
| `PLATFORM_RATE_LIMIT_PER_MINUTE` | `600` | 每个调用方每分钟调用上限（429 + `Retry-After`） |
| `PLATFORM_AUDIT_KEEP` | `20000` | 审计表保留行数上限（超出删最旧） |
| `PLATFORM_SOURCE_DB` | 空 | 数据源：**默认空=中台自举**（`source_spec.py`）；填 Agent 演示库路径才复用（本地便利，容器里通常没有该文件） |
| `PLATFORM_REPLENISH_QTY_THRESHOLD` / `PLATFORM_REPLENISH_AMOUNT_THRESHOLD` | `200` / `50000` | 补货人工确认阈值 |

## 5. 重试、降级与并发（统一在 `src/app/resilience.py`）

「这个错误该不该重试」只有一处实现：`is_retryable()`（类名 → HTTP 状态码 → 文本信号，
**不含裸数字**，所以业务文案「500 件」不会被当成 HTTP 500）。业务/参数/鉴权类错误一律不重试、
不降级；已经吐出流式增量的失败一律不重试（否则前端会收到两段答案）。

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_MAX_RETRIES` | `2` | 模型调用的**网络/限流**重试次数（瞬时错误；尊重 `Retry-After`） |
| `LLM_JSON_REPAIR_RETRIES` | `2` | 模型返回结构不符时的**修复重试**次数（与网络重试分开配置） |
| `CHAT_MAX_RETRIES` | `1` | 对话侧瞬时失败重试次数（流式已出增量时不重试） |
| `CHAT_FALLBACK_MODEL` | 空 | 主模型**可重试失败**且重试额度用尽后的备用模型；空=不降级 |
| `CHAT_MAX_CONCURRENCY` | `4` | 进程内同时进行的对话轮次上限（含该轮的溯源重写调用） |
| `CHAT_TURN_TIMEOUT_SECONDS` | `300` | 单轮硬超时：超时抛 `TurnBudgetExceeded`，不让一次请求无限占住连接与并发槽 |
| `CHAT_RETRY_BUDGET_SECONDS` | `90` | 单轮重试/降级总预算，用尽即上抛（重试与降级是乘法的，没有预算会拖住并发槽十几分钟） |
| `CHAT_GLOBAL_QPS` | `0`（关闭） | 全局每秒对话请求上限（1 秒滑窗）：多用户同时涌入时挡整体流量，超限 429 |
| `CHAT_TOOL_CALL_BUDGET` | `15` | **单轮成功工具调用次数上限**（工具循环守护，P1-1）。超了立即中止本轮并返回
`工具调用陷入循环/已达上限` + 错误码 + 追踪标记；manifest 的 `max_tool_calls` 优先。依据：演示库 137 轮真实对话里
单轮工具调用中位 2~3、最大 11 |
| （工具循环阈值，固定值） | 失败 3 次 / 同工具同入参 3 次 | 连续失败 3 次、或同一工具 + 同一输入指纹连续成功 3 次 → 判定陷入循环并中止（`TOOL_LOOP_FAILURES` / `TOOL_LOOP_REPEAT`）。
`/api/health.limits` 暴露这三项（`tool_call_budget` / `tool_failure_limit` / `tool_repeat_limit`） |
| `INSPECT_COLLECT_TIMEOUT_SECONDS` | `45` | 巡检**单个数据源**的采集超时（P2-6）：竞品/知识库失败→整轮 degraded 继续；ERP 失败→本 SKU 记 failed 并写明原因 |
| `CHAT_SESSION_LOCK_REQUIRED` | `true` | 会话锁后端（Redis）不可用时：`true`→**503 fail-closed**（不放弃"同一会话一次一轮"）；`false`→降级为进程内锁并告警 |
| （会话锁 TTL，无单独变量） | `max(LLM_TIMEOUT_SECONDS, CHAT_TURN_TIMEOUT_SECONDS) + 30s` | 派生值：必须覆盖最坏单轮，否则多副本下锁会中途过期 → 同一会话被并发写 L1 记忆。实现见 `chat_routes._session_lock_ttl_ms()` |
| `INSPECT_TIMEOUT_SECONDS` | `600` | 单 SKU 研判**硬上限**（超时抛 `InspectTimeoutError`，本轮记 failed）。图的校验循环最多 `MAX_REVISIONS+1` 轮，不设上限时最坏可达数十分钟。`0`=不限（不推荐） |
| （巡检顺序锁 TTL，无单独变量） | `max(LOCK_TTL_MS, INSPECT_TIMEOUT_SECONDS + 60s)` | 派生值：锁必须比最坏研判活得久，否则多副本会重复研判同一 SKU。实现见 `patrol._inspect_lock_ttl_ms()` |
| `LLM_MAX_CONCURRENCY` | `4` | 非对话链路（巡检/研判/溯源重写）模型调用上限 |
| `MCP_MAX_RETRIES` | `2` | MCP 调用重试次数（超时/协议错误；业务错误不重试、不缓存） |

> 真实上限 ≈ `CHAT_MAX_CONCURRENCY + LLM_MAX_CONCURRENCY`（两套闸门互不知情，见
> `docs/ARCHITECTURE.md` 的并发一节；`/api/health.limits` 会给出两部分各自的实际在跑数）。

## 6. 记忆与规划

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEMORY_MAX_INLINE_ROWS` | `50` | L0 工具结果内联行数上限；超过只回 summary + `artifact_path` |
| `MEMORY_FACTS_TRIGGER_MESSAGES` / `MEMORY_FACTS_TRIGGER_TOKENS` | `12` / `4000` | L1 事实卡触发阈值 |
| `MEMORY_FACTS_USE_LLM` | `false` | 事实卡默认纯规则抽取（可复现） |
| `ANALYSES_DIR` / `ARTIFACTS_DIR` | `data/analyses` / `data/artifacts` | L3 档案与 L0 明细目录 |
| `ANALYSIS_MERGE_MODE` | `merge` | 档案主文档策略：`merge`（同问题的多次分析合并，叙述由 LLM 整合）/ `keep-both`（只保留两份 + 待人工合并段，不调模型）/ `unique`（一次分析一个文件，不合并） |
| `ANALYSIS_MERGE_INLINE` | `false` | 摘要整合是否在本次工具调用内同步执行（默认后台跑，不占本轮答复时间） |
| `ANALYSIS_MERGE_TIMEOUT_SECONDS` | `20` | 同步整合的超时上限（超时即留给离线脚本 `scripts/merge_analyses.py`） |
| `ANALYSIS_MERGE_MAX_RETRIES` | `3` | 哈希 CAS 反复冲突时的重试次数，用尽则保留两份 + 进 `_pending` 队列 |
| `ANALYSIS_LOCK_WAIT_SECONDS` | `0.2` | 档案短锁等待上限（临界区是毫秒级：读哈希 → 校验 → 原子替换） |
| `ANALYSIS_PENDING_LIMIT` | `20` | 单次 drain 处理多少条待合并 |
| `QUERY_PLAN_ENABLED` | `true` | 对话前生成结构化 query_plan（确定性，成本≈0） |
| `QUERY_REWRITE_LLM` | `false` | 生成式整句改写默认关闭（只写入不替换原句） |
| `QUERY_PLAN_CONFIDENCE_THRESHOLD` | `0.7` | 置信度低于该值、且确实缺实体（SKU/款号）时最多追问一次；口径解释/巡检/审批清单不追问 |
| `CHAT_RETENTION_HOURS` | `24` | 历史会话保留时长（自动清理消息/运行详情/计划/记忆） |
| `CHAT_RATE_LIMIT_PER_MINUTE` | `20` | 每用户每分钟对话轮次上限，超过 429 + Retry-After |
| `WRITE_TOOLS_ROLES` | `["admin"]` | 写工具白名单（JSON 数组）：建补货草稿/售后工单/沉淀档案；admin 始终允许 |
| `AGENTS_ENABLED` | `true` | 多 Agent 总开关：置 `false` 后**只走默认 Agent**（显式 `?agent_id=` 与关键词/intent 路由都不生效，`run_detail.agent.routed_by="agents_disabled"`）；状态同时回显在 `/api/health.agents.enabled` |
| `AGENTS_REGISTRY_DIR` | 空 | Agent manifest 目录，空=`src/app/agents/registry` |
| `AGENTS_HOT_RELOAD` | `true` | 目录/提示词变更即热重载（每轮对话前比对「目录 mtime + 文件最大 mtime」；只盯目录会漏掉「改已有文件」） |
| manifest 的 `tools` | 缺省 ["read"] | 缺省只读；全量需显式 ["*"]；提示词占位符写错会让该 Agent 加载失败 |
| manifest 的 `max_tool_calls` | `15`（1~40） | 该 Agent 单轮成功工具调用预算（P1-1）。仓库内取值：analyst 15 / diagnostician 12 / operator 6 |
| manifest 的只读语义 | 未声明 `write` 组 | 声明里没有 `write`（也不是 `*`）的 Agent 会被**剔除写工具**并在 `agents.errors` 留一条 warning（P2-4），
被剔除的清单见 `/api/agents` 的 `stripped_write_tools` |
| `REVOCATION_CACHE_SECONDS` | `30` | 撤销令牌「未撤销」结果的缓存秒数（登出仍立即生效） |
| `HEALTH_CACHE_SECONDS` | `10` | `/api/health` 线程计数缓存秒数（前端会轮询） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 空 | 配了才导出 OTLP span（Langfuse/Jaeger/Tempo）；未装 SDK 自动跳过 |
| `OTEL_SERVICE_NAME` | `sku-agent` | span 的 service.name |
| `GROUNDING_ENABLED` | `true` | 出口溯源校验：带单位数字必须能追溯到本轮 `result_id` 或知识库 `chunk_id`，不达标重写一次，仍不达标标记 degraded |

## 7. 演示与定时

| 变量 | 默认 | 说明 |
|---|---|---|
| `SCHEDULER_ENABLED` | **`false`** | 定时巡检默认关闭（开启后会真实调 LLM，无人访问也耗 token）；单次演练用 `POST /api/scheduler/run-once` |
| `SCHEDULER_HOURLY_LIMIT` / `SCHEDULER_DAILY_LIMIT` | `5` / `50` | 定时巡检每次处理的 SKU 上限 |

改完阈值后重跑 `python scripts/run_screening.py` 即可验证筛选结果。

## 8. 其它生效但一般不用调的变量

> 这些都在 `Settings` 里有默认值、按环境变量读取；列出来是为了「排障时知道去改哪个」，日常不需要动。

| 变量 | 默认 | 说明 |
|---|---|---|
| `METRIC_VERSION` | `2026Q1` | 口径版本号：写进 `query_plan` 与分析档案（front-matter），保证结论可复现；中台与 Agent 两侧对齐 |
| `LLM_TIMEOUT_SECONDS` | `120` | 非对话链路（研判/巡检/溯源重写）单次模型调用超时 |
| `PATROL_CONCURRENCY` | `3` | 巡检并发处理的 SKU 数（受 `CHAT_MAX_CONCURRENCY` 之外的独立闸门限制） |
| `PLATFORM_TIMEOUT_SECONDS` | `15` | Agent → 中台单次 HTTP 超时 |
| `SECURITY_RECORD_WINDOW_HOURS` | `4` | 安全记录（登录 IP/时间）保留窗口 |
| `CACHE_TTL_PLAN` | `1800` | `query_plan` 缓存 TTL（秒） |
| `CATALOG_TTL_SECONDS` | `300` | SKU 参考目录（部门映射用的元数据）缓存 TTL |
| `CATALOG_SYNC_ON_STARTUP` | `true` | 启动时与中台对齐能力目录/参考目录（失败只告警，不阻塞启动） |
| `DEPARTMENT_MAPPING_CACHE` | `data/department_map.json` | 部门映射本地缓存文件（中台为准 + 指纹缓存） |
| `STREAM_KEY` / `STREAM_GROUP` / `STREAM_CONSUMER` | `patrol:tasks` / `patrol` / `worker-1` | 巡检 Redis Stream 的 key / 消费组 / 消费者名（多副本时每个副本要不同 consumer） |
| `LOCK_TTL_MS` | `120000` | 分布式锁基准 TTL；研判锁按「研判硬上限 + 60s」自动取更大值 |
| `PLATFORM_PROXY` | 空 | 访问中台是否走 HTTP 代理（空=直连） |
| `PLATFORM_CONTRACT_STRICT` | `false` | 契约校验严格度（true=中台缺 `schema_version` 也算错，生产建议打开） |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `TIMEZONE` | `Asia/Shanghai` | 定时任务与「今天」口径所用时区 |
| `PLATFORM_CACHE_TTL` / `PLATFORM_CACHE_JITTER` / `PLATFORM_API_PORT` | `300` / `0.2` / `8001` | **中台侧**的参数（`src/data_platform/config.py`）：读接口缓存 TTL、TTL 抖动比例、监听端口 |


---

## 生图 Agent 配置（`IMAGE_*`）

生图 Agent 有独立 settings（`app.agents_image.config.ImageSettings`），env 前缀 `IMAGE_`，与 SKU 配置同读仓库根 `.env`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `IMAGE_ENABLED` | `true` | 总开关（false 时不挂 `/api/image/*`、不建表、不起 MCP 会话） |
| `IMAGE_PROVIDER` | `fake` | 生图后端：`fake`（占位图，零成本）/ `volcengine`（后置） |
| `IMAGE_PRODUCT_SOURCE` | `fake` | 参数与商品来源：`fake`（离线替身）/ `mcp`（经 MCP 取中台数据） |
| `IMAGE_STORAGE` | `local` | 图片存储：`local`（24h 清理）/ `cos`（后置） |
| `IMAGE_DISPATCHER` | `local` | 任务执行：`local`（进程内）/ `scf`（后置） |
| `IMAGE_MAX_REFS` | `6` | 每任务参考图合计上限（来自中台商品图） |
| `IMAGE_MAX_BATCH_PRODUCTS` | `5` | 一次批量最多选几个商品 |
| `IMAGE_MAX_TOTAL_IMAGES` | `9` | 单任务最多出图张数 |
| `IMAGE_CONCURRENCY` | `8` | 本地调度并发 |
| `IMAGE_TIMEOUT_SECONDS` | `60` | 单任务超时 |
| `IMAGE_RETENTION_HOURS` | `24` | 本地图片（refs + outputs）保留时长 |
| `IMAGE_CLEANUP_INTERVAL_MINUTES` | `60` | 清理间隔（0 = 不启动后台清理） |
| `IMAGE_AUTH_SECRET` | 仓库默认值 | 令牌签名密钥，**上线必须换**（启动会告警） |
| `IMAGE_TOKEN_TTL_HOURS` | `8` | 令牌有效期 |
| `IMAGE_LOGIN_MAX_ATTEMPTS` / `IMAGE_LOGIN_WINDOW_SECONDS` | `8` / `300` | 登录限流 |
| `IMAGE_DEFAULT_QUOTA_PERIOD` / `IMAGE_DEFAULT_QUOTA_IMAGES` | `day` / `50` | 新账号默认配额 |
| `IMAGE_CRITIC_PASS_SCORE` | `75` | Critic 通过线 |
| `IMAGE_REFLEXION_MAX_ROUNDS` | `2` | 反思重写轮数上限 |
| `IMAGE_RAG_ENABLED` / `IMAGE_RAG_TOP_K` | `true` / `2` | 本地知识库召回开关与条数 |
| `IMAGE_MCP_TIMEOUT_SECONDS` / `IMAGE_MCP_RETRIES` | `5` / `2` | MCP 调用超时与重试（超限直接 503，不降级） |
| `IMAGE_PLATFORM_URL` / `IMAGE_PLATFORM_TOKEN` | `http://127.0.0.1:8001` / 空 | 中台地址与令牌（令牌留空沿用 `PLATFORM_INTERNAL_TOKEN`） |
| `IMAGE_COST_PER_IMAGE_CNY` / `IMAGE_COST_BUDGET_CNY` | `0` / `0.5` | 成本口径（fake 为 0） |
| `ARK_API_KEY` / `ARK_IMAGE_MODEL` / `ARK_VISION_MODEL` | 空 | 火山方舟（后期验收门再填） |

数据中台侧（`data_platform`）：`PLATFORM_DB_URL`、`PLATFORM_INTERNAL_TOKEN`、`PLATFORM_API_PORT`（默认 8001）。


---

## v7 新增配置（生图 Agent）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ACCOUNTS_SOURCE` | `db` | 账号来源；`legacy` 回滚到 SKU 常量账号（故障回退） |
| `IMAGE_REFS_PER_ITEM` | `2` | 每个「商品+颜色」取几张参考图（主图 + 细节） |
| `IMAGE_MAX_TASKS_PER_REQUEST` | `60` | 颜色×平台×变体的任务数上限 |
| `IMAGE_PROVIDER_CONCURRENCY` | `10` | 调第三方生图 API 的并发上限（与任务并发分开） |
| `IMAGE_RECOVERY_INTERVAL_SECONDS` | `60` | 恢复看门狗扫描间隔（0=关闭） |
| `IMAGE_COMPLIANCE_GATE` | `main` | 交付前像素门禁：`main`（只拦主图）/`all`/`off` |
| `IMAGE_COMPLIANCE_AUTOFIX` | `true` | 门禁不过时先归白/裁方/放大再复检 |
| `IMAGE_COST_BUDGET_CNY` | `0.5` | 单次请求成本估算上限（估算用 `IMAGE_COST_PER_IMAGE_CNY`；真实成本取 provider） |

平台标识：**淘系 / 抖系**（`通用` 仅作中台兜底规则卡）。
