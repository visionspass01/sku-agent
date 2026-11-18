# 接口一览

> Agent 服务：`http://127.0.0.1:8000/api/*`（`/api/health`、`/api/metrics` 等公开读接口匿名可看；其余按账号/部门作用域）。
>
> **匿名可读范围**（演示期入口设计，`optional_user`）：`/api/metrics`、`/api/skus`、`/api/skus/{id}`、
> `/api/anomalies`、`/api/rag/search`、`/api/queue/stats`、`/api/cache/breakdown`、`/api/scheduler/status`、
> **`/api/inspections/{run_id}`**、**`/api/patrol/{run_id}`**。匿名按「全部门只读」渲染（登录账号按部门过滤）；
> `run_id` 是 uuid，不可枚举。
> 写操作与 `/api/patrol/recent`、`/api/chat/*`、账号/安全记录仍需登录；生产化时把入口收敛到 `require_user`
> （见 `docs/ARCHITECTURE.md` §4）。
> 数据中台：`http://127.0.0.1:8001/api/v1/*`（需 `X-Internal-Token`，Agent 内部调用）。
> 完整 OpenAPI 见运行后的 `/docs`；**浏览器可读版**是 `/api.html`（同源取 `/openapi.json` 自行渲染）。
> FastAPI 自带的 `/docs` 依赖 cdn.jsdelivr.net 的 Swagger UI 资源，而本项目的 CSP 是 `default-src 'self'`，
> 外链会被浏览器拦掉 → 页面全空，所以站内导航统一指向 `/api.html`；`/docs` 只在本地用 curl/放宽 CSP 时才有意义。

## 1. Agent 服务（:8000）

### 1.1 健康与指标

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 服务状态 + `database` / `cache` / `vector_store` / `embedding` 四个后端（含 degraded 原因）+ `checkpoint`（saver/线程数/上限/多副本 warning）+ `mcp`（连接池/熔断）+ `platform`（中台熔断 `breaker.state`）+ `limits`（对话并发/限流/**真实上游并发**/`active_streams`/`global_qps`）+ `prompts`（提示词版本）+ `analysis_dedupe`（档案唯一索引/待合并）+ `runtime`（进程内状态与多副本前置条件）+ `observability`（OTel 是否开启）+ **`rag_conflicts`**（口径冲突计数：checked/ok/none/unavailable/intra/suppressed/**skipped**/**overrides**/**scope_mismatch**/**scope_partial**/**comparison**/**multi_department**/`resolved_rate`）+ **`rag_facts`**（语料口径覆盖率：docs_with_facts/facts/docs_with_triggers/**docs_without_facts**/**department_gaps**/unmatched_sections）+ **`security`**（部署自检：`secret_is_default` / `platform_token_is_default` / `ok` / `note`，只返回布尔不泄露值）+ `features` + 静态资源版本 |
| GET | `/api/metrics` | 看板指标：SKU/异常数、缓存命中率、LLM 调用/token/成本、队列、**工具可靠性（tools，含成功/重试/超时 + 失败分类 ok/business_error/transport_error/timeout/breaker_open/**loop_aborted**）**、延迟分位（latency）、**检索栈 `rag.conflicts`** |
| GET | `/api/metrics/agents?hours=24` | per-agent 画像：轮次 / 延迟分位 / token 与成本 / 工具调用 / 降级率 / grounding（部门账号只看本部门） |
| GET | `/api/metrics/trajectory?hours=24` | **trajectory 指标（E6）**：`tool_calls_per_turn{avg,p50,p95,max}`、`llm_calls_per_turn{…}`、`repeat_call_rate`（同工具同入参重复占比，代理指标）、`turns_with_guard`/`budget_utilization`、`aborted_turns`/`abort_rate`/`error_codes`（来自中止台账，跨重启）、`per_agent`、`top_tools`、`multi_turn{sessions,followup_turns,followup_judged,followup_unjudged,followup_success_rate}`（`grounding_status=disabled/error` 视为未判定，不进成功率分母）、`process.loop_aborted_since_start`（进程内计数）；部门账号只看本部门 |
| GET | `/api/queue/stats` | 巡检队列：`length` / `pending` / `departments` / `tracking_deliveries`（正在跟踪投递次数的消息数）/ `dead`（进死信的条数） |
| GET | `/api/cache/breakdown` | Agent 旁路缓存 + 中台缓存的命中明细与 TTL |

### 1.2 登录与安全

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | `{username, password}` → `token` + `user`（HMAC，默认 8h）；同「账号+IP」60s 内失败 5 次返回 **429 + Retry-After** |
| GET | `/api/auth/me` | 当前账号 |
| POST | `/api/auth/logout` | 注销：token 写入持久化黑名单（`revoked_token` 表），**重启后仍失效** |
| GET | `/api/auth/security-records` | 近 4 小时登录记录（账号 / IP / 时间 / 结果 `ok|failed`，按账号或管理员看全部） |

### 1.3 监控（只读）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/skus`、`/api/skus/{sku_id}` | SKU 列表 / 详情（部门过滤） |
| GET | `/api/anomalies` | 异常 SKU 列表（量化筛查结果） |
| GET | `/api/rag/search?q=&topk=` | SOP 混合检索（向量 + BM25 双路召回 → RRF 融合 → rerank），返回 `chunk_id`/分数/引用；`citations[]` 带 `authority`/`is_primary`；另有 `conflicts`（含 `kind`：`cross_doc`/`intra_doc`）、`conflict_check`（`ok`/`none`/`unavailable`）、`conflict_diagnostics`（`compared_keys`/`single_source_keys`/`suppressed`/`candidates`/`skipped_reason`/**`facets={departments, asked, missing, comparative}`**/`overrides`/`uncovered_departments`）、**`department_overrides`**（无部门面 / 多部门比较 / 部分可见 / 跨部门比较意图时的部门例外，只作补充说明）、**`scope_notice`**（顶层字段，四种触发：完全不可见 / 部分可见 / 多部门比较 / 跨部门比较意图）、**`uncovered_departments`**（未单独规定口径、按通用执行的部门）；`scope` 在问句含部门面时收敛为「X 部门内部 SOP + 通用 SOP（按问题部门收敛）」 |
| GET | `/api/inspections/{run_id}` | 单次研判状态 + 结果 + 进度 |
| GET | `/api/patrol/recent`、`/api/patrol/{run_id}` | 批量巡检日志 / 单次详情 |
| GET | `/api/scheduler/status` | 定时任务状态（默认关闭） |

### 1.4 动作

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/inspect` | 单 SKU 研判（后台任务，返回 `run_id`；`inspection_run.params.thread_id` 供追问） |
| POST | `/api/inspect/followup` | **多轮追问**：`{sku_id, instruction, thread_id?}`，复用上一轮采集数据（MCP 调用增量 0）；`thread_id` 与 state 的 SKU 不一致 → **403**（防跨部门读取） |
| POST | `/api/patrol` | 批量巡检：`{mode: hourly\|daily, limit}`，Stream 幂等入队 |
| POST | `/api/patrol/{run_id}/cancel` | 协作式终止批量巡检 |
| POST | `/api/scheduler/run-once?job=hourly\|daily` | 单次演练定时任务（管理员） |

### 1.5 对话

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/chat` | 同步对话：`{session_id?, message, include_query_plan?, agent_id?}` → `reply` + `tool_trace` + `run_detail` + `agent`；超频返回 **429 + Retry-After**；工具循环/预算/步数超限返回 **422**（见 §1.5.1） |
| POST | `/api/chat/stream` | **SSE 流式对话**：进度事件 `stage` / `llm_start` / `llm_end` / `tool_start` / `tool_end` / `loop_aborted` / **`delta_retract`**（模型先输出文本、随后把同一条消息变成工具调用时撤回那段过程文字：`{reason:"tool_call", chars}`）/ `tick`，**答案增量** `delta`（`{text}`，逐段追加），结束 `done`（完整响应体，含 `run_detail.grounding`）。失败时 `error` 事件可能带 `error_code` / `trace_id` / `fatal`（见 §1.5.1） |
| GET | `/api/agents` | **Agent 注册表**：可用 Agent 列表（id/name/version/prompt/model/tools/**max_tool_calls**/**stripped_write_tools**/routing）+ 默认 Agent + 加载错误（`level=warning|error`）；支持热插拔 |
| POST | `/api/admin/agents/reload` | 管理员手动重载注册表与提示词（改完 manifest/md 立即生效，无需重启） |
| GET | `/api/chat/sessions` | 最近 24h 会话（自动清理过期会话） |
| GET | `/api/chat/{session_id}` | 历史消息（含当时的 `run_detail` 快照） |
| POST | `/api/chat/{session_id}/reset` | 清空会话消息 |

### 1.5.1 会话级错误契约：循环 / 预算 / 步数（P1-2）

这三类失败**不是**"接口坏了"，而是"这一轮需要换个问法"：它们有确定的错误码、带追踪标记，
并且**不应该被客户端自动重发**（重发只会把同一个循环/预算再撞一次）。

| 错误码 | 含义 | 触发点 |
|---|---|---|
| `TOOL_LOOP_REPEAT` | 同一工具 + 同一输入指纹连续成功 3 次 | 工具循环守护（`app/agents/tool_guard.py`） |
| `TOOL_LOOP_FAILURES` | **链路类**失败（transport / ratelimited / timeout / breaker_open）连续 3 次；业务错误（不存在/无权限/参数错/契约漂移）不计入、且会打断计数链 | 同上 |
| `TOOL_BUDGET_EXCEEDED` | 单轮成功工具调用达上限（`manifest.max_tool_calls`，缺省 `CHAT_TOOL_CALL_BUDGET=15`） | 同上（超预算的那次调用**不会执行**） |
| `TURN_BUDGET_EXCEEDED` | 单轮重试/降级预算用尽，或 300s 硬超时 | `_invoke_agent` / `_run_chat` |
| `AGENT_STEP_LIMIT` | 图推理步数超限（`recursion_limit`，按 `3×预算 + 6` 绑定；hook 是图上独立节点，一轮工具循环 3 超步） | LangGraph（**只在预算没拦住时**才会触发，见 ARCHITECTURE §5.11.1） |
| `AGENT_INVALID_TOOL_NAME` | 模型点了**不存在的工具名**（Multi-Agent 下工具集按 Agent 裁剪时最易发生）。服务端先做**一次纠错重试**（把可用工具名写进系统提示）；仍失败才返回本错误。`detail` 里带「不存在的工具 + 本轮可用工具」，`run_detail.agent.invalid_tool_retries` 记纠错次数 | `tool_guard.validate_tool_calls`（`post_model_hook`）+ `_invoke_agent` |
| `AGENT_INVALID_TOOL_ARGS` | 模型给出的**工具参数不是合法 JSON**（`AIMessage.invalid_tool_calls`）。服务端同样先纠错重试一次（把可用工具与参数要求写进系统提示），仍失败才返回本错误，不会退化成"空答复 + 兜底文案" | 同上（`reason=invalid_args`） |
| `STREAM_INTERRUPTED` | **流式已推过增量**后才失败（模型/网络中断等）：同样标 `fatal`，避免客户端把"半截答案 + 失败"当成传输抖动而自动降级重发（那会把整轮再跑一遍） | `chat_routes.chat_stream` 的失败分支（`partial_deltas>0`） |

**非流式（`POST /api/chat`）**：HTTP **422**，`detail` 形如

```
工具调用陷入循环，请再次重试（错误码 TOOL_LOOP_REPEAT｜追踪 t_ab12cd34ef）
```

同时带响应头 `X-Error-Code` / `X-Trace-Id`（`trace_id` 在轮次内等价于 `run_detail.turn_id`）。

**流式（`POST /api/chat/stream`）**：`error` 事件

```json
{"message": "工具调用陷入循环，请再次重试（错误码 TOOL_LOOP_REPEAT｜追踪 t_…）",
 "status": 422, "error_code": "TOOL_LOOP_REPEAT", "trace_id": "t_…",
 "fatal": true, "retryable": true}
```

`fatal=true` 是给客户端的契约：**不要**把它当传输层失败去降级重发（前端 `web/app.js` 据此
不触发 POST 回退）。中止前已经流出的 `delta` 保留在气泡里，并同时收到 `loop_aborted` 事件
（进度卡会先把阶段切成"本轮已中止"）。

**观测**：`/api/health.limits` 暴露 `tool_call_budget` / `tool_failure_limit` / `tool_repeat_limit`；
`run_detail.guard` 给出本轮预算用量（`max_calls` / `success_calls` / `failure_streak` / `aborted`）；
工具统计新增结果分类 `loop_aborted`（见 `/api/metrics.tools.overall.outcomes` 与各 `tools[…]` 条目；`/api/metrics.mcp` 只报连接池/熔断，不含结果分类）。

### 1.6 人工确认（HITL）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/actions/list`、`/api/actions/pending` | 近 4 小时 / 待确认补货草稿单（部门过滤） |
| POST | `/api/actions/{approval_id}/approve`、`/reject` | 确认 / 驳回（Mock ERP 状态流转并留痕） |
| GET | `/api/actions/after-sales?status=pending\|approved\|rejected\|all` | 售后工单列表（部门过滤，含审批账号/确认时间/批注） |
| POST | `/api/actions/after-sales/{ticket_id}/approve`、`/reject` | 售后人工确认闭环；已处理再批返回 **409**，跨部门 **403** |

### 1.7 记忆（四层）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/memory/sessions/{session_id}` | 会话记忆快照：事实卡、工作集（+可展示文本）、最近 query_plan、取数台账。**仅会话所有者或管理员**（他人 403） |
| GET/PUT | `/api/memory/profile` | **用户偏好画像**（输出偏好 / 关注 SKU，字段白名单，仅本人） |
| GET | `/api/memory/analyses?q=&department=` | L3 组织记忆检索（BM25 + 部门过滤；档案可见范围 = 本部门 + 「全员」） |
| POST | `/api/memory/analyses` | 手动沉淀分析档案（部门留空 = 全部门通用；部门账号只能写本部门） |
| GET | `/api/memory/analyses/{analysis_id}` | 档案全文（跨部门 404） |

> **档案的部门归属（复审 F1）**：对话里的 `save_analysis` 工具**按本轮取过的 SKU/款号/部门级入参
> 自动推断部门**，并在返回里给出 `department_source`（`inferred_sku` / `inferred_style` /
> `inferred_arg` / `user` / `all`）与 `department_note`（例如"目录查不到所属部门"）。
> 只有跨部门或公司级结论才落「全员」（= 全部门可见）；历史存量档案不迁移。

## 2. 数据中台（:8001，治理下沉后的调用契约）

**请求头**

| 头 | 必需 | 说明 |
|---|---|---|
| `X-Internal-Token`（或 `X-Api-Key` / `Authorization: Bearer`） | 是（`/health` 除外） | 中台按令牌映射到**主体**：`agent_id` + `scopes` + `departments`。多 Agent 用 `PLATFORM_API_KEYS` 一 Agent 一份 |
| `X-Agent-Id` | 建议 | 调用方自述（与令牌解析出的一致性由中台记录在审计里） |
| `X-Request-Id` | 建议 | 关联 id；中台会原样回显，并写进审计行（Agent 侧 `run_detail.steps[].request_id` 可对账） |

**响应头**：`X-Request-Id`、`X-Agent-Id`、`X-Platform-Contract`（契约版本）、`X-Data-Version`、`X-RateLimit-Limit`、`X-RateLimit-Remaining`；限流时额外 `Retry-After`。
每个响应体都带 `schema_version`（见 `src/data_platform/contract.py`）；主版本不兼容时调用方应**直接失败**而不是继续解析。

**错误语义**

| 状态码 | 含义 | Agent 侧归类（`error_kind`） |
|---|---|---|
| 401 | 缺少/未知令牌 | `auth`（不重试、不计入熔断） |
| 403 | 缺 scope，或 SKU/审批单不属于调用方部门 | `permission`（不重试、不计入熔断） |
| 429 | 超过该调用方配额（1 分钟滑窗） | `ratelimited`（带 `retry_after_s`，不原地重试） |
| 409 | 审批单/售后单已被他人处理（条件更新 rowcount=0） | `transport`（业务冲突，不重试） |
| 404 | SKU/审批单/订单/款式不存在 | `transport` |
| 2xx 但 `schema_version` 主版本不符 | 接口契约升级 | `contract`（不重试、不计熔断，提示联系管理员） |

| 方法 | 路径 | 需要 scope | 说明 |
|---|---|---|---|
| GET | `/api/v1/health` | 公开 | 中台健康（含 `data_version`；带令牌时回显 `caller`） |
| GET | `/api/v1/metadata/metrics` | read | 治理口径元数据（含公式/同义词与 `contract`，注入 Agent Prompt） |
| GET | `/api/v1/metadata/departments` | read | **部门 ↔ 品类映射的唯一真相**（带内容指纹 `version`，Agent 启动时同步） |
| GET | `/api/v1/metadata/capabilities` | read | **能力目录**：工具分组 / 所需 scope / 部门作用域种类 / 落哪些端点（Agent 工具集唯一真相） |
| GET | `/api/v1/metadata/catalog` | read | SKU 参考目录（仅标识与分类，按部门过滤、分页）——Agent 侧作用域判定用它 |
| GET | `/api/v1/metadata/synonyms` | read | 检索同义词（RAG 查询改写用；改词表不用发 Agent 版本） |
| POST/GET/PATCH | `/api/v1/agents`、`/agents/me`、`/agents/{id}`、`/{id}/rotate`、`/{id}/disable` | `admin`（例外：`GET /api/v1/agents` 只要 `read`，`/agents/me` 任意已认证） | **运行时注册 Agent**：明文令牌只返回一次、只存 sha256；改 scope/轮换/禁用即时生效 |
| GET | `/api/v1/skus/{id}/inventory`、`/metrics`、`/sell-through`、`/in-transit`、`/reservations`、`/orders`、`/sales`、`/sales-series`、`/return-risk`、`/transfers` | read | SKU 维度取数（**按部门校验**）；`sales-series` 补齐零销量日 |
| GET | `/api/v1/skus/batch?sku_ids=a,b,c` | read | 批量取指标（≤50 个；跨部门条目剔除并计数，不整批失败） |
| GET | `/api/v1/orders/{id}`、`/orders/{id}/logistics` | read | 订单详情（含物流 + 售后摘要）/ 物流轨迹 |
| POST | `/api/v1/after-sales`；GET `/after-sales`；POST `/after-sales/{id}/approve|reject` | write / read / approve | 售后工单：幂等建单（同订单+原因+数量只一单）、按部门过滤、条件更新（重复确认 409） |
| GET | `/api/v1/suppliers/{id}`、`/platform-activities`、`/styles/{id}/competitors|price-band|heat` | read | 供应商交期/MOQ/准时率、大促窗口、竞品同款行情 |
| GET | `/api/v1/styles/{id}/metrics` | read | **款级**指标：生命周期/尺码曲线/齐码断码/周转/售罄 + 款×色×码明细 |
| GET | `/api/v1/metrics/size-curve?style_id=`、`/size-gap`、`/apparel-overview` | read | 尺码曲线 / 断码款清单（含缺失码）/ 部门齐码率·断码率·周转·售罄 |
| GET | `/api/v1/metrics/sales-rate` | read | 动销率：带 `department` 时校验权限；不带时按调用方可见范围聚合（`effective_departments`） |
| POST | `/api/v1/erp/replenish-orders` | write | 创建补货草稿单（大额→人工确认）；`idempotency_key` 同键只建一单（并发冲突也返回 `deduped: true`，不再 500）；返回 `caller` |
| GET | `/api/v1/approvals?status=pending` | read | 待确认补货单（**按部门过滤**，带 `scope_filtered` / `scope_dropped` / `count`） |
| POST | `/api/v1/approvals/{id}/approve`、`/reject` | approve | **条件更新**：已被处理 → 409（含当前状态与处理人）；返回 `caller` 与 `previous_status` |
| POST | `/api/v1/transfer-suggestions` | read | 跨仓调拨建议（按部门校验） |
| GET | `/api/v1/etl/runs` | etl | 模拟 Airflow 运行日志（DAG/重试/耗时；含物流/售后/竞品/供应链接入任务） |
| GET | `/api/v1/audit?limit=&agent_id=` | etl | **调用审计**：谁在什么时候调了什么、决策（allow/denied_*/rate_limited/conflict）与原因 |

## 3. 订单 / 售后（MCP 工具，中台 REST 的适配层）

MCP server 现在**不碰任何数据库**：它们是「工具名 → `/api/v1/...`」的映射层（`src/app/mcp_servers/_platform.py`），
以 `X-Agent-Id: mcp-<server>` 的身份进中台审计与配额。协议（memory/stdio/direct）、连接池、熔断都保留。

| 工具 | 上游端点 | 说明 |
|---|---|---|
| `get_order_status(order_id)` | `GET /orders/{id}` | 订单状态 + 物流摘要 + 售后工单 |
| `list_orders(sku_id, days)` | `GET /skus/{id}/orders` | 某 SKU 近 N 天订单 |
| `get_logistics(order_id)` | `GET /orders/{id}/logistics` | 承运商 / 运单号 / 状态 / 预计到货 |
| `get_return_risk(sku_id)` | `GET /skus/{id}/return-risk` | 退货率 + 近期订单 + 售后工单量（≥18% 高风险） |
| `create_after_sales_proposal(order_id, reason, qty)` | `POST /after-sales` | 幂等售后草稿单（`pending_confirmation`），确认入口见 1.6 |
| ERP 7 个（库存/销量/在途/调拨/同款尺码/供应商/大促） | `/skus/batch`、`/sales-series`、`/in-transit`、`/transfers`、`/styles/{id}/metrics`、`/suppliers/{id}`、`/platform-activities` | 返回结构保持旧契约 |
| 拍立淘 3 个（同款/价格带/热度） | `/styles/{id}/competitors|price-band|heat` | 竞品行情归中台治理 |

## 3.1 对话工具的部门作用域（27 个工具）
权限**不是**靠提示词自觉，而是工具元数据 + 单一守卫执行（`src/app/security/scope.py` +
`TOOL_SCOPES` 声明表）。新增工具若不在表里声明作用域，`build_platform_tools` 装配直接失败——
「漏声明」在启动期就暴露，不会留到运行期变成越权面。无 `sku_id` 入参的工具（如 `list_pending_approvals`）
由声明表 + 返回体过滤（`scope_filtered` / `scope_dropped`）覆盖，不依赖"参数里有没有 sku_id"。

| 作用域 | 工具 | 执行方式 |
|---|---|---|
| `sku` | `get_inventory` `get_sell_through` `get_sku_metrics` `get_in_transit` `get_reservations` `get_orders` `get_sales` `list_orders` `create_replenishment_order` `create_transfer_suggestion` | 入参 `sku_id` 的品类必须属于当前账号部门，否则返回 `error_kind=business` |
| `order` | `get_order_status` `get_logistics` `create_after_sales` | `order_id` → SKU → 部门校验 |
| `sku_list` | `list_pending_approvals` | 返回体 `items` 按 `sku_id` 过滤；带 `scope_filtered` / `scope_dropped` |
| `style` | `get_style_metrics` `get_size_curve` | 入参 `style_id` 的品类必须属于当前账号部门，否则返回 `error_kind=business` |
| `kb` | `search_knowledge` | 知识库按「本部门内部 SOP + 通用 SOP」过滤（`kb_department_filter`），管理员不受限 |
| `department` | `get_sales_rate` `get_size_gap` `get_apparel_overview` | 部门账号的 `department` 强制回本部门；**当模型填了别的部门**时返回体带 `department_forced=true` 与说明 note；admin 透传 |
| `org` | `search_analyses` `read_analysis` `save_analysis` | 按 `departments` 参数在函数体内过滤 |
| `global` | `get_metric_definitions` | 全局只读口径元数据 |
| `local` | `list_skills` `load_skill` `plan_query` | 本地文件 / 纯计算，不涉及业务数据 |

装配顺序固定：`scope_guard`（作用域）→ 写权限拒绝桩（`WRITE_TOOL_NAMES` 仅 `WRITE_TOOLS_ROLES` 里的角色可调，缺省 admin）→
`with_result_contract`（`result_id` / `as_of` / `summary` / 台账）→ 最外层 `with_tool_loop_guard`（同工具同参 / 连续失败 / 超预算即中止本轮；无轮次上下文时完全惰性）。
工具目录与作用域声明可通过 `tool_catalogue()` 查看（含 `scopes` 字段）。

## 4. 与前端相关的响应字段（容易被问）

| 字段 | 位置 | 含义 |
|---|---|---|
| `run_detail.timeline[]` | `/api/chat`、`/api/chat/stream`(done) | 本轮时间轴：`kind=stage|llm|tool` + `offset_ms` + `elapsed_ms`，回答「为什么等了几秒」 |
| `run_detail.phases` | 同上 | `llm_ms` / `tools_ms` / `other_ms` / `total_ms` / 调用次数 |
| `run_detail.grounding` | 同上 | 出口溯源校验：`status`（passed/skipped/repaired/degraded/disabled/error）、`issues`、`citations.unknown`、`numbers.unverified`、`elapsed_ms`。**空答复**（模型没给正文）时固定为 `skipped` 且 `note` 写明原因 |
| `run_detail.answer` | 同上 | 复审 R3：这一轮到底"答没答"。`is_fallback=true` 表示模型没给正文、已用兜底文案；`chars` 是答复字数；顶层 `reply_empty` 是同一事实的简写（前端/评测/台账都用它，不再靠"grounding=skipped"反推） |
| `run_detail.llm.token_source` / `retry_calls` | 同上 | token 归因来源（`callback`=按调用回调采集，含重试轮；`messages`=只统计进了最终消息的调用）与"没进最终消息的调用次数"（纠错/备用模型重试） |
| `run_detail.llm.cost_cny` | 同上 | 本轮成本估算（按 `LLM_PRICE_INPUT/OUTPUT` 单价） |
| `run_detail.llm.model` / `model_requested` / `model_fallback_used` | 同上 | **实际**使用的模型 / 请求的模型 / 是否动用了备用模型；与 `run_detail.agent.model_used` 一致 |
| `run_detail.prompt.name` / `version` | 同上 | 本轮**真正渲染**的提示词模板名与版本（per-agent 不同）；`prompt.chat_system` 为兼容字段（默认模板版本） |
| `run_detail.permission` | 同上 | 部门作用域、允许范围、失败/越权步骤。`allowed` 优先取 `query_plan.allowed_departments`，语义是**部门名**（admin/匿名 → null=不限；部门账号 → 本部门）；没有查询计划时（如 `QUERY_PLAN_ENABLED=false`）回落为账号可见**品类**列表（女装账号即 `["女装","童装"]`）。`query_plan.entities.asked_departments` 是问句涉及的部门（部门名或品类名都会归一到所属部门） |
| `streamed` | 同上 | 本轮是否真的走了 token 流式（前端据此判断气泡是否已增量显示） |
| `streamed_unverified` | 同上 | 流式初稿可能被出口闸门改写过（`repaired`/`degraded`/`error`）→ 前端提示「刚才那版是未校验初稿」 |
| `run_detail.grounding.streamed_before_check` | 同上 | 同上口径的细粒度标记：初稿是否已经流给用户之后才做的校验 |
| `run_detail.agent` | 同上 | 本轮由哪个 Agent 执行：`id/name/version/prompt_version/model_requested/model_used/model/fallback_used/tools/tool_count/tools_all/memory_scope/max_reflection_rounds/routed_by/fallback_from/retries/invalid_tool_retries`；`model_used` 是**实际**使用的模型（回退时与 `model_requested` 不同）；`invalid_tool_retries` 是"模型点了不存在的工具名 → 服务端纠错重试"的次数（0 表示没发生） |
| `run_detail.plan.needs_clarify` / `clarify` | 同上 | 本轮查询计划是否要求追问、追问内容（`missing` 列表）。**口径**：只有"缺实体（SKU/款号）且置信度低"才追问；口径解释/巡检/审批清单、以及带部门品类面的聚合问题不追问 |
| `run_detail.steps[].artifact_path` | 同上 | 单次工具结果超过内联上限（`MEMORY_MAX_INLINE_ROWS=50` 行）时，明细落 `data/artifacts/<会话>/<result_id>.jsonl`，**回给模型的只有前 3 行 + `row_count`**；artifact 面向人工/前端/审计，**Agent 不回读**（详见 `docs/ARCHITECTURE.md` L0 小节） |
| `run_detail.references.session_reference` | 同上 | 复审 S1：`@session` 引用解析的结果 `{status, token, count}`。`status` ∈ `none`（本轮没引用）/ `ok` / `denied`（存在但不可见）/ `not_found`（id 不存在或标题没命中）。**失败也留痕**——否则审计只能看到"这轮没引用"，看不出用户引用过且没找到 |
| 系统提示里的 `<session-reference status="…">` | — | 同一状态会以标签形式进模型上下文：`denied` 写明"不能引用、不要拿别的档案替代"，`not_found` 写明"不要编造它的内容"。**`token` 是用户原文，进提示前已白名单化（去尖括号/引号/结构提示词）+ 截断 60 字**（复审 T1），原始 token 仍在 `run_detail.references.session_reference.token` 里供审计 |
| `search_analyses` 工具的 `matched` | — | 复审 S1：`keyword`（关键词命中）/ `recent_fallback`（关键词没命中，返回的是**最近的档案**，`note` 会提示模型如实说明） |
| `run_detail.llm.prompt_cache` | 同上 | 上下文缓存命中：`hit_tokens/input_tokens/hit_rate` |
| `run_detail.guard` | 同上 | 工具循环守护（P1-1）：`max_calls` / `success_calls` / `failure_streak` / `repeat_streak` / `aborted`；正常轮次也能核对"这轮用了几次预算" |
| `grounding.status=error` / `TurnBudgetExceeded` | 同上 | 单轮硬超时（`CHAT_TURN_TIMEOUT_SECONDS`）或重试预算（`CHAT_RETRY_BUDGET_SECONDS`）用尽时的明确报错 |

## 5. 并发写与状态字段（容易被问「两个请求同时改会怎样」）

| 场景 | 现在的结果 | 字段/契约 |
|---|---|---|
| 同一会话两轮并发对话 | 第二个请求立刻 **409**（不再并发跑，避免覆盖 L1 记忆） | `POST /api/chat`、`/api/chat/stream` 的 409 |
| 两个 agent 同时写同一份分析档案 | 路径按 topic 派生（同问题一份主文档）；写时短锁 + 哈希 CAS；冲突保留「原文档/新文档」快照 + 合并 | `data/analyses/_merge_log.jsonl`；`/api/memory/analyses/{id}` 的 `document_analysis_ids` / `merge_count` |
| 档案去重（同内容重复沉淀） | 内容指纹去重；有唯一索引后并发也不产生重复行 | `/api/health.analysis_dedupe`（`unique_index` / `duplicates` / `pending_merges`） |
| 并发巡检筛查 | 抢 `lock:screen:<小时>`，抢不到就复用库里现成结果 | 巡检 summary 的 `screened_by_us` |
| 多副本部署 | 进程内计数（限流/闸门）会按副本放大；SQLite checkpoint 单写者 | `/api/health.runtime.in_process_state`、`checkpoint.warning` |


---

## 生图 Agent（`/api/image/*`）

约定：除 `/health` 外全部需要 `Authorization: Bearer <token>`（独立账号体系，与 SKU 的 `/api/auth` 无关）。
错误统一返回 `{detail, error_code, trace_id, retryable}`，并在响应头带 `X-Error-Code` / `X-Trace-Id`（成功响应也带 `X-Trace-Id`）。

### 用户侧

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/image/auth/login` | `{username,password}` → `{token,expires_in,user}`；失败限流 429 + `Retry-After` |
| GET | `/api/image/auth/me` | 当前账号 |
| POST | `/api/image/auth/logout` | 令牌立即失效（jti 黑名单） |
| GET | `/api/image/products?keyword=&limit=` | 商品清单（转发 MCP `image_list_products`） |
| GET | `/api/image/catalog?platform=&category=` | 构图 / 场景风格 / 图位策略 / 平台规则 |
| POST | `/api/image/tasks` | `{request_id?, product_ids[1..5], prompt, compositions?, scene_styles?, n?, aspect?, platform?}` → `{batch_id, tasks[], slots, limits}` |
| GET | `/api/image/tasks?batch_id=` | 我的任务列表（管理员 `?all=true`） |
| GET | `/api/image/tasks/{id}` | 任务快照（含 `slots / prompt_pack / refs / assets / backfill_status`） |
| GET | `/api/image/tasks/{id}/stream` | SSE：`snapshot → stage → asset → backfill → done` |
| POST | `/api/image/tasks/{id}/cancel` | 取消（已结束 409 `IMG_TASK_CONFLICT`） |
| GET | `/api/image/assets/{id}` | 取图（本人或管理员；本地存储走鉴权接口） |
| GET | `/api/image/quota` | 我的配额（管理员返回 `unlimited`） |
| GET | `/api/image/health` | 健康（provider / product_source / 并发 / 计数） |

### 管理员侧

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/image/admin/metrics?granularity=day&count=30&username=` | 折线桶（含按账户细分）+ 5 张卡 + outbox 状态 |
| GET | `/api/image/admin/tasks?username=&status=&limit=` | 全量任务 |
| GET | `/api/image/admin/tasks/{id}` | 任务详情（Prompt 包、图位、回写状态） |
| GET/POST/PUT | `/api/image/admin/users` · `/users/{username}` | 账号列表 / 新建 / 改密码或启停 |
| GET/PUT | `/api/image/admin/quota` · `/quota/{username}` | 配额列表 / 授权（day/week/month/unlimited） |
| POST | `/api/image/admin/replay-outbox` | 重放回写失败的 outbox |
| POST | `/api/image/admin/reload` | 热加载 manifest / prompt |
| POST | `/api/image/admin/reindex` | 重建 `data/kb_image` 索引 |

### 错误码

`IMG_UNAUTHORIZED`(401) · `IMG_FORBIDDEN`(403) · `IMG_LOGIN_THROTTLED`(429) · `IMG_INPUT_INVALID`(422) ·
`IMG_BATCH_TOO_LARGE`(422) · `IMG_PRODUCT_NOT_FOUND`(404) · `IMG_REFERENCE_REQUIRED`(422，所选商品无可用图) ·
`IMG_TOO_MANY_REFS`(422) · `IMG_QUOTA_EXCEEDED`(429) · `IMG_UPSTREAM_MCP_UNAVAILABLE`(503，中台不可用直接失败) ·
`IMG_UPSTREAM_INVALID`(502) · `IMG_CONTENT_BLOCKED`(422) · `IMG_SCHEMA_INVALID`(422) · `IMG_CRITIC_FAILED`(422) ·
`IMG_BACKFILL_FAILED`(502) · `IMG_TASK_NOT_FOUND`(404) · `IMG_TASK_CONFLICT`(409) · `IMG_TIMEOUT`(504)


---

## v7 接口变化（生图 Agent）

| 变化 | 说明 |
|---|---|
| `POST /api/image/tasks` 新增字段 | `product_colors[]` / `all_colors`、`slot_count`、`image_count`、`platforms[]` / `platform_variant`、`mode`(standard/variant/iterate)、`Idempotency-Key` 头 |
| `POST /api/image/tasks` 响应 | `{batch_id, tasks[{task_id,product_id,color,color_key,platform,variant_id,image_count,slot_count}], slots, planned_images, idempotent}` |
| `GET /api/image/tasks/{id}` | 增加 `platform/variant_id/color_key/slot_count/image_count/copy`；`assets[]` 带 `signed_url/adopt_status/blocked_reason/purged` |
| `POST /api/image/tasks/{id}/cancel` | 未调模型 → 200「未调用生图模型的任务请求已取消。」；已调模型 → 409 `provider_in_flight=true` |
| 新增 | `POST /tasks/{id}/assets/{aid}/adopt` · `/reject{reason}` · `/unblock`；`GET /admin/samples` · `/admin/feedback` |
| `GET /api/image/assets/{id}` | 支持 `?exp=&sig=` 短签名（5 分钟），给 `<img src>` 用；过期/篡改 403 |
| `GET /api/image/tasks` | 支持 `limit` / `offset` 分页 |
| 错误码新增 | `IMG_REFS_BUDGET`(422，颜色×参考图超预算)、`IMG_COST_BUDGET_EXCEEDED`(429，预算闸) |
