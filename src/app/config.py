"""SKU 监管 Agent：配置中心。

所有可调参数集中于此，业务阈值不进 Prompt，便于运营侧按品类调参。
环境变量优先级最高，其次为项目根目录 .env 文件。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[2]  # src/app/config.py -> 仓库根
DATA_DIR = BASE_DIR / "data"
KB_DIR = DATA_DIR / "kb"
STATIC_DIR = BASE_DIR / "web"  # 前端资产独立在 web/（原 app/static）

# ---------------------------------------------------------------- 公开默认密钥
# 这两个值是仓库里公开可见的演示默认值。部署时若不覆盖，任何人都能按常量伪造 Token /
# 以全权身份调用中台，因此启动时告警、并在 /api/health.security 暴露是否仍为默认值。
DEFAULT_AUTH_SECRET = "change-me-demo-secret"
DEFAULT_PLATFORM_INTERNAL_TOKEN = "demo-internal-token"

# ---------------------------------------------------------------- 部门与数据域
# 演示假设：童装款归属「女装部门」（可在下方按需调整映射）
# 注意：这是**本地默认值**。唯一的真相在中台（`/api/v1/metadata/departments`），
# Agent 启动时会拉取并覆盖（见 `set_department_categories`），离线时沿用这里的默认值。
DEPARTMENT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "男装": ("男装",),
    "女装": ("女装", "童装"),
}
ALL_DEPARTMENTS: tuple[str, ...] = tuple(DEPARTMENT_CATEGORIES)
DEPARTMENT_LABELS = {"男装": "男装部门", "女装": "女装部门"}
COMMON_SOP_DEPARTMENT = "通用"


def all_departments() -> tuple[str, ...]:
    """当前生效的部门列表（随 `set_department_categories` 变化）。"""
    return tuple(DEPARTMENT_CATEGORIES)


def set_department_categories(mapping: dict, version: str = "") -> list[str]:
    """用中台下发的映射覆盖本地默认值；返回生效的部门名列表。

    为什么允许覆盖：部门 ↔ 品类映射属于「治理口径」，中台是唯一真相。
    两侧各写一份必然会漂移（评审 2.2-6），所以 Agent 启动时同步一次，离线时兜底用默认值。

    `version` 是中台下发的**内容指纹**：同步成功就落盘（`data/cache/departments.json`），
    下次启动先读磁盘再拉中台。这样中台短暂不可用时不会悄悄退回硬编码默认值
    （早先的表现是「重启后映射回退、部门隔离按错的品类生效」）。
    """
    global ALL_DEPARTMENTS, _DEPARTMENT_VERSION
    cleaned = {
        str(name): tuple(str(cat) for cat in (cats or []) if cat)
        for name, cats in (mapping or {}).items()
        if cats
    }
    if not cleaned:
        return sorted(DEPARTMENT_CATEGORIES)
    DEPARTMENT_CATEGORIES.clear()
    DEPARTMENT_CATEGORIES.update(cleaned)
    ALL_DEPARTMENTS = tuple(DEPARTMENT_CATEGORIES)
    _DEPARTMENT_VERSION = version or _fingerprint(DEPARTMENT_CATEGORIES)
    _persist_department_categories()
    return sorted(DEPARTMENT_CATEGORIES)


_DEPARTMENT_VERSION = ""


def _fingerprint(mapping: dict) -> str:
    import hashlib
    import json

    payload = json.dumps({k: sorted(v) for k, v in mapping.items()}, ensure_ascii=False,
                         sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def department_cache_file() -> Path:
    return DATA_DIR / "cache" / "departments.json"


def _persist_department_categories() -> None:
    import json

    try:
        if not get_settings().department_mapping_cache:
            return
        path = department_cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({
            "version": _DEPARTMENT_VERSION,
            "departments": {k: list(v) for k, v in DEPARTMENT_CATEGORIES.items()},
            "synced_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:  # 落盘失败不影响进程内生效
        pass


def load_cached_department_categories() -> dict:
    """启动时先读磁盘缓存（中台不可用也不至于退回硬编码默认值）。"""
    import json

    path = department_cache_file()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    mapping = payload.get("departments") or {}
    if mapping:
        set_department_categories(mapping, str(payload.get("version") or ""))
    return {"mapping": mapping, "version": str(payload.get("version") or ""),
            "synced_at": payload.get("synced_at", "")}


def department_mapping_status() -> dict:
    return {
        "version": _DEPARTMENT_VERSION or _fingerprint(DEPARTMENT_CATEGORIES),
        "departments": {k: list(v) for k, v in DEPARTMENT_CATEGORIES.items()},
        "cached_file": str(department_cache_file()),
        "note": "唯一真相在中台 /api/v1/metadata/departments；本地为带指纹的缓存副本",
    }


def categories_for(department: str | None) -> list[str] | None:
    if not department:
        return None
    return list(DEPARTMENT_CATEGORIES.get(department, ()))


def department_of(category: str | None) -> str | None:
    for dept, cats in DEPARTMENT_CATEGORIES.items():
        if category in cats:
            return dept
    return None


def department_label(department: str | None) -> str:
    return DEPARTMENT_LABELS.get(department or "", department or "全部门")


class ScreeningThresholds(BaseSettings):
    """多维度量化筛选阈值（业务脚本口径，与 LLM 无关）。"""

    model_config = SettingsConfigDict(env_prefix="SCREEN_", extra="ignore")

    window_hours: int = 7 * 24  # 特征窗口：近 7 天小时销量
    min_daily_base: float = 3.0  # 销量基数门槛：日均 >= 3 件才参与趋势判定
    trend_slope_norm: float = -0.45  # 归一化线性回归斜率阈值（下降 45%+）
    trend_r2_min: float = 0.55  # 趋势拟合优度下限
    trend_recent_ratio: float = -0.20  # 后 3 天 vs 前 4 天动销比（辅助确认下滑）
    days_of_supply_blocked: float = 60.0  # 库存可供天数 >= 60 视为积压候选
    days_of_supply_severe: float = 90.0  # 可售天数 >= 90 直接判定积压（不依赖趋势）
    days_of_supply_low: float = 7.0  # 可供天数 <= 7 视为缺货候选
    return_rate_high: float = 0.18  # 退货率 >= 18% 视为异常
    zero_size_min: int = 2  # 同款断码尺码数 >= 2 触发断码
    season_boundary_days: int = 30  # 距换季节点 <= 30 天进入换季观察窗


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- LLM ----
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-flash"
    llm_timeout_seconds: float = 120.0
    llm_max_tokens: int = 4000
    llm_temperature: float = 0.3
    # deepseek-flash 是推理模型：关闭思维链可把单次调用从 ~15s 降到 ~8s
    llm_disable_thinking: bool = True
    llm_max_retries: int = 2        # 网络/限流类失败的重试次数（瞬时错误）
    # JSON 结构不符时的「修复重试」次数：与网络重试分开配置，
    # 否则 LLM_MAX_RETRIES 会被误读成「网络重试」而实际只作用于 JSON 修复（早先的坑）
    llm_json_repair_retries: int = 2
    llm_max_concurrency: int = 4
    # 成本估算单价（元 / 百万 tokens）：用于看板「本轮/累计成本」，按合同价在 .env 覆盖即可。
    # 默认取 deepseek-flash 的量级（输入 1 元 / 输出 4 元每百万 tokens），只作量级参考。
    llm_price_input: float = 1.0
    llm_price_output: float = 4.0

    # ---- 存储 ----
    database_url: str = f"sqlite:///{(DATA_DIR / 'sku_demo.db').as_posix()}"
    redis_url: str = ""
    # Redis 读超时：redis-py 默认 socket_timeout=None = **无限等**。
    # 同步客户端的读操作跑在事件循环里（缓存/队列），一次挂起会冻住整个进程
    # （SSE 心跳、所有会话），所以必须显式设上限。
    redis_socket_timeout: float = 3.0
    redis_socket_connect_timeout: float = 3.0
    redis_health_check_interval: int = 30

    # ---- MCP ----
    # memory：进程内 MCP JSON-RPC（默认，受限环境可用）；stdio：真实子进程协议；direct：函数直调（测试）
    mcp_transport: str = "memory"
    mcp_call_timeout: float = 30.0
    mcp_max_retries: int = 2
    mcp_pool_size: int = 2  # 每个 MCP server 的会话数（>1 时同 server 并行取数不再排队）
    mcp_breaker_threshold: int = 3  # 同 server 连续失败几次后熔断
    mcp_breaker_reset_seconds: float = 60.0  # 熔断保持时长，到点放一次请求试探

    # ---- 缓存 ----
    cache_ttl_mcp: int = 600
    cache_ttl_rag: int = 600
    cache_ttl_plan: int = 1800
    # Agent 侧「工具取数」旁路缓存：对话里重复问同一 SKU/指标时直接命中，不再打数据中台。
    # 只缓存只读 GET；写操作（补货/调拨/审批）永不缓存。
    cache_ttl_platform: int = 120
    # 负缓存：中台「连不上」这类链路故障的短 TTL（秒）。没有它的话，中台挂掉时
    # 每一轮对话都要先把 platform_timeout_seconds 等满再降级（15s × 每轮）。
    cache_ttl_negative: int = 10
    cache_jitter_ratio: float = 0.2  # TTL 随机偏移上限（规避缓存雪崩）

    # ---- 向量检索（可选：默认内存 + 本地哈希向量；服务器可切 Qdrant + 腾讯 embedding）----
    # memory：进程内 numpy 余弦（默认，零依赖，当前 67 块语料毫秒级）
    # qdrant：外部向量库（collection 维度由 embedding 模型决定，可重建）
    vector_store: str = "memory"
    qdrant_url: str = ""  # http://qdrant:6333，或 local[:<path>] 走 qdrant 本地模式（开发/测试）
    qdrant_collection: str = "kb_chunks"
    qdrant_timeout_seconds: float = 3.0
    qdrant_candidate_n: int = 64  # 向量候选数（≥ 语料块数时与内存模式结果等价）
    # hash：本地确定性哈希向量（512 维，离线兜底，永远保留在 SQLite）
    # tencent：腾讯云 TokenHub 文本向量（OpenAI 兼容；0.6b=1024 维 / 4b=2560 维）
    embedding_backend: str = "hash"
    tencent_embedding_api_key: str = ""
    tencent_embedding_base_url: str = "https://tokenhub.tencentmaas.com/v1"
    tencent_embedding_model: str = "kinfra-text-embedding-0.6b"
    embedding_timeout_seconds: float = 8.0
    embedding_max_retries: int = 2
    # ---- RAG 融合 / 无答案 / 重排（评测见 scripts/eval_rag.py --ablation）----
    # RRF 的 k：`score = Σ 1/(k+rank)`；k 越大越"平滑"，60 是业界常用默认
    rag_rrf_k: int = 60
    # 无答案判定：向量余弦 < min_vector（且覆盖率 < min_coverage）→ 判「知识库没有」；
    # 但若 BM25 最高分 ≥ strong_keyword（词法证据很强），则不弃答。
    # 阈值在本地哈希向量 + 当前 14 篇语料上标定（正样本最低 vec 0.276 / 负样本最高 0.236）；
    # 换 embedding 或语料大改后请重跑 `python scripts/eval_rag.py --negatives` 重新校准。
    rag_min_vector_score: float = 0.25
    rag_min_coverage: float = 0.25
    rag_min_keyword_score: float = 15.0
    # 同一个文档最多保留几条（避免一篇 SOP 的相邻块占满 top-k）
    rag_max_per_doc: int = 2
    # MMR 权重：1.0=纯相关度，越小越强调多样性
    rag_mmr_lambda: float = 0.7
    # 重排：none / heuristic（离线默认） / llm（生产默认，走 DeepSeek listwise） / cross
    rag_rerank_backend: str = "heuristic"
    rag_rerank_max_candidates: int = 30
    rag_rerank_timeout_ms: int = 400
    rag_rerank_model: str = "BAAI/bge-reranker-base"
    # 索引一致性：启动时按「每篇文档的块指纹」对账，只重建不一致的文档（不整体重建）
    rag_index_reconcile: bool = True
    rag_index_warmup: bool = True
    embedding_batch_size: int = 64  # TokenHub 建议单批 ≤128
    embedding_concurrency: int = 2
    # 知识库证据进「溯源校验」时的保留上限（字符）。
    # 原先工具结果统一截 500 字，SOP 里的阈值/天数常常排在第 3、4 段，
    # 于是有依据的回答反而被判定「数字无处可溯」。KB 单条结果改成这个上限。
    kb_evidence_max_chars: int = 6000

    # ---- 巡检/队列 ----
    stream_key: str = "sku:patrol:stream"
    stream_group: str = "sku-patrol-workers"
    stream_consumer: str = "consumer-1"
    # 重投与死信：consume 用 ">" 只能拿到「新消息」，进了 PEL 却没 ACK 的消息
    # （进程崩溃 / 研判异常退出）再也不会被投递——表现为「pending 一直涨、SKU 少跑了」。
    queue_min_idle_ms: int = 60_000   # 闲置多久的 PEL 条目可以回收
    queue_max_deliveries: int = 3     # 同一条消息最多投递几次，超过进死信
    queue_claim_batch: int = 20       # 单次回收条数上限
    patrol_concurrency: int = 3
    # 单 SKU 研判的**硬上限**（秒）：超时抛 `InspectTimeoutError`，本轮记 failed。
    # 为什么必须有：图的校验循环最多 `MAX_REVISIONS+1` 轮、每轮 5~6 次 LLM 调用，
    # 单次 120s 时最坏是几十分钟——既占住并发槽，也会让顺序锁中途过期（多副本重复研判）。
    # 实测单次研判约 14.5s，600s 留了 ~40x 余量；置 0 表示不设上限（不推荐）。
    inspect_timeout_seconds: float = 600.0
    # 单 SKU 研判的顺序锁 TTL **下限**。实际 TTL 取 max(本值, 研判硬上限 + 60s)，
    # 见 `patrol._inspect_lock_ttl_ms()`——只调小这里不会让锁变短。
    lock_ttl_ms: int = 120_000
    # Demo 默认关闭：定时巡检会真实调用 LLM（每小时 5 个 + 每日 09:00 共 50 个 SKU），
    # 无人使用时也会持续产生 token 消耗。需要演示定时能力时置 SCHEDULER_ENABLED=true。
    scheduler_enabled: bool = False
    scheduler_hourly_limit: int = 5  # 定时小时增量巡检的 SKU 上限
    scheduler_daily_limit: int = 50  # 定时每日全量巡检的 SKU 上限

    # ---- 数据规模（演示默认 300 SKU × 30 天） ----
    seed_skus: int = 300
    seed_days: int = 30
    seed: int = 42

    # ---- 认证（演示账号固定；生产请接企业统一登录）----
    # 默认值即「公开默认密钥」：一旦部署仍用它，任何人都能按仓库里的常量伪造 Token，
    # 因此启动时会告警、并写入 /api/health.security.secret_is_default（见 DEFAULT_* 常量）。
    auth_secret: str = DEFAULT_AUTH_SECRET
    auth_token_ttl_hours: int = 8
    security_record_window_hours: int = 4

    # ---- 数据中台（Agent 通过 REST 调用，不直连中台库）----
    platform_api_url: str = "http://127.0.0.1:8001"
    platform_internal_token: str = DEFAULT_PLATFORM_INTERNAL_TOKEN
    # MCP 通道专用令牌（可选）：留空则 MCP Server 复用 platform_internal_token。
    # 之前 `mcp_servers/_platform.py::_token()` 已经在读它，但 Settings 缺字段 → 环境变量被
    # pydantic 的 extra="ignore" 静默丢弃，配了也不生效；补上字段后才是真开关。
    # 想让 MCP 在中台侧独立分账（独立主体/独立配额），把该 token 也登记进 PLATFORM_API_KEYS。
    platform_mcp_token: str = ""
    # 调用方身份：中台按它做授权/配额/审计；一个中台可以接多个 Agent，各有各的 key 与 scope
    platform_agent_id: str = "sku-agent"
    platform_timeout_seconds: float = 15.0
    # 中台熔断：连续链路失败几次就快速失败（与 MCP 熔断同一套实现，见 app/resilience.py）。
    # 没有它时，中台整体不可用会表现为「每个工具调用都先等满超时再报错」。
    platform_breaker_threshold: int = 3
    platform_breaker_reset_seconds: float = 60.0
    # 契约校验严格度：False=缺少 schema_version 视为 legacy 1.0（兼容既有部署/测试桩，只计数）；
    # True=缺少版本号也当错误（生产建议打开，让"中台漏版本号"在灰度期就暴露）。
    # 主版本不一致无论开关都会直接失败，见 app/tools/platform_contract.py。
    platform_contract_strict: bool = False
    # 部门映射缓存（中台是唯一真相）：同步成功写 data/cache/departments.json，
    # 下次启动先读磁盘再拉中台，避免「中台短暂不可用 → 悄悄退回硬编码默认值」。
    department_mapping_cache: bool = True
    # SKU 参考目录（元数据）缓存：用于 Agent 侧部门作用域二次判定，替代本地 sku 表
    catalog_ttl_seconds: float = 300.0
    catalog_sync_on_startup: bool = True
    # 显式代理（默认空=不走代理）。注意：httpx 默认 trust_env=True 会读系统代理，
    # 本机配了代理软件时连 127.0.0.1 也会被绕进代理（实测每次新建 client 多 0.9s）。
    platform_proxy: str = ""
    chat_history_limit: int = 12
    # ---- 分析线程持久化（LangGraph checkpointer）----
    # sqlite：AsyncSqliteSaver 落 data/checkpoints.sqlite（默认，重启后追问仍可用）
    # redis：需安装 langgraph-checkpoint-redis 且 Redis 可达，否则自动回退 sqlite
    checkpoint_backend: str = "sqlite"
    checkpoint_path: str = ""  # 空=与业务库同目录（sqlite）或 data/checkpoints.sqlite
    checkpoint_max_threads: int = 200  # 线程上限，超出按最近使用时间淘汰最旧
    # 淘汰保护窗口（复审 F2）：最近这么多秒内被 touch 过的线程不参与淘汰——
    # 长研判（可达 INSPECT_TIMEOUT_SECONDS）期间可能只有"开始时"那一次 touch。
    checkpoint_prune_min_age_seconds: float = 600.0
    # ---- 登录限流（防爆破）----
    login_rate_limit: int = 5  # 同一账号+IP 在窗口内允许的失败次数
    login_rate_window_seconds: float = 60.0  # 滑动窗口长度（秒）
    # 撤销令牌的「未撤销」结果缓存秒数：避免每个鉴权请求都查一次库；
    # 登出会先写进程内黑名单，因此不受这里影响（立即失效）
    revocation_cache_seconds: float = 30.0
    # /api/health 的线程计数缓存秒数（前端会轮询 health，避免每次都 COUNT）
    health_cache_seconds: float = 10.0
    # ---- 对话限流与并发（防打爆上游 LLM）----
    # 每用户每分钟最多几轮对话；超过返回 429 + Retry-After（前端提示稍后重试）
    chat_rate_limit_per_minute: int = 20
    # 进程内同时进行的对话轮次上限：超出排队等待，避免把 DeepSeek 打成 429
    chat_max_concurrency: int = 4
    # 全局 QPS 保护（0=关闭）：多用户同时涌入时挡住整体流量，而不是只按用户限流
    chat_global_qps: int = 0
    # 会话锁后端（配了 REDIS_URL 时的跨副本锁）不可用怎么办：
    # True（默认，fail-closed）→ 503，绝不放弃「同一会话一次一轮」的保证；
    # False → 降级为进程内锁并告警（多副本下仍可能并发覆盖 L1 记忆，仅演示/兜底用）。
    chat_session_lock_required: bool = True
    # ---- 读/写分离（写工具白名单）----
    # 有副作用的工具（建补货草稿单、售后工单、沉淀档案）只对这些角色开放；
    # admin 永远允许。演示想放宽可设为 ["admin", "user"]，但默认收紧更接近生产。
    write_tools_roles: list[str] = ["admin"]
    # ---- Agent 注册表与热插拔 ----
    agents_enabled: bool = True
    agents_registry_dir: str = ""  # 空 = src/app/agents/registry
    agents_hot_reload: bool = True   # 目录/提示词变更即热重载，不需要重启
    # ---- 对话模型重试与降级 ----
    chat_max_retries: int = 1        # transient 失败（超时/限流/连接）重试次数
    chat_fallback_model: str = ""    # 重试仍失败时的备用模型，空=不降级
    # 单轮硬超时与重试预算：重试/降级是乘法的（2 模型 × 2 次 × LLM_TIMEOUT），
    # 没有预算时一轮最坏能占住一个并发槽十几分钟。预算耗尽就上抛，把时间还给用户。
    chat_turn_timeout_seconds: float = 300.0
    chat_retry_budget_seconds: float = 90.0
    # ---- 工具循环守护（P1-1）----
    # 单轮允许的**成功**工具调用次数（manifest.max_tool_calls 优先）。超了立即终止本轮并
    # 提示「工具调用陷入循环，请再次重试」——没有它，模型"卡住"时会在 300s 硬超时前
    # 反复调工具（每轮一次完整 LLM 调用，成本线性放大）。
    chat_tool_call_budget: int = 15
    # ---- 巡检采集（P2-6）----
    # 每个数据源的采集超时：竞品/知识库失败降级继续，ERP 失败则本 SKU 判失败（缺基准不硬出结论）
    inspect_collect_timeout_seconds: float = 45.0
    # ---- OpenTelemetry（可选，默认关闭）----
    # 配了标准环境变量 OTEL_EXPORTER_OTLP_ENDPOINT 才导出 span
    # （Langfuse / Jaeger / Tempo 都吃 OTLP）；未装 SDK 时自动跳过。字段名与官方 env 对齐。
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "sku-agent"
    # 历史会话保留时长：超过该小时数的会话（含消息/运行详情/计划/记忆）在列举时自动清理
    chat_retention_hours: int = 24
    replenish_qty_threshold: int = 200
    replenish_amount_threshold: float = 50000.0

    # ---- 四层记忆（L0 工作记忆 / L1 会话记忆 / L2 工作区 / L3 组织记忆）----
    memory_max_inline_rows: int = 50  # 单次工具返回内联行数上限，超过只回 summary + artifact
    memory_summary_chars: int = 1500  # 工具 summary / 事实卡字符上限
    memory_facts_trigger_messages: int = 12  # 消息数超过该值即触发事实卡压缩
    memory_facts_trigger_tokens: int = 4000  # 历史字符数 > tokens×4 时触发事实卡
    memory_facts_use_llm: bool = False  # 事实卡默认纯规则抽取（可复现），置 True 才让 LLM 归纳
    memory_retain_ratio: float = 1.0  # 原文保留比例（1.0=保留最近 chat_history_limit 条）
    memory_context_chars: int = 3000  # 注入系统提示的记忆上下文上限
    memory_prune_levels: list[int] = [4096, 2048, 512]  # 工具结果回放三档剪枝（dsh pruner 等价）
    memory_public_workspace_file: str = "AGENTS.md"  # L2 基线文件（项目根）
    analyses_dir: str = ""  # L3 档案目录，空= data/analyses
    # ---- L3 档案主文档的并发写（CAS + 合并）----
    # merge：同问题的多次分析合并进一份主文档（结构由代码合并，叙述由 LLM 整合）
    # keep-both：只保留两份文档 + 「待人工合并」段落，不调模型
    # unique：退化为「一次分析一个文件」（不合并，也永不覆盖）
    analysis_merge_mode: str = "merge"
    analysis_merge_inline: bool = False      # 默认异步合并，避免把模型耗时压进 agent 轮次
    analysis_merge_timeout_seconds: float = 20.0
    analysis_merge_max_retries: int = 3      # CAS 反复冲突时的重试次数，用尽则保留两份 + pending
    analysis_lock_wait_seconds: float = 0.2  # 短锁等待上限（临界区是毫秒级，等不到就不覆盖）
    analysis_pending_limit: int = 20         # 单次 drain 处理多少条待合并
    artifacts_dir: str = ""  # L0 明细落地目录，空= data/artifacts
    # ---- 查询理解与规划（query_plan）----
    query_plan_enabled: bool = True  # 是否在对话前生成查询计划（确定性解析，成本≈0）
    query_rewrite_llm: bool = False  # 生成式整句改写默认关闭（只做结构化计划，不替换原句）
    query_plan_confidence_threshold: float = 0.7  # 低于该值最多追问一次
    metric_version: str = "2026Q1"  # 口径版本，写入 plan / 分析档案，保证可复现
    # ---- 出口溯源校验（grounding）----
    grounding_enabled: bool = True  # 答案数字与 result_id 对齐校验（硬规则；不通过时重写一次）

    # ---- 其它 ----
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    screening: ScreeningThresholds = Field(default_factory=ScreeningThresholds)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def artifacts_path(self) -> Path:
        """L0 明细落地目录（默认 data/artifacts，随 data 卷持久化）。"""
        return Path(self.artifacts_dir) if self.artifacts_dir else DATA_DIR / "artifacts"

    @property
    def analyses_path(self) -> Path:
        """L3 组织记忆档案目录（默认 data/analyses/<部门>/）。"""
        return Path(self.analyses_dir) if self.analyses_dir else DATA_DIR / "analyses"

    @property
    def workspace_baseline_file(self) -> Path:
        """L2 工作区记忆基线（AGENTS.md）。"""
        return BASE_DIR / self.memory_public_workspace_file

    def ensure_dirs(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        KB_DIR.mkdir(parents=True, exist_ok=True)
        self.artifacts_path.mkdir(parents=True, exist_ok=True)
        self.analyses_path.mkdir(parents=True, exist_ok=True)


settings = Settings()

# 配置注入点：测试/脚本可覆写（避免单例到处 import 造成无法替换）
_override: Settings | None = None


def get_settings() -> Settings:
    return _override or settings


def set_settings(new_settings: Settings) -> None:
    global _override
    _override = new_settings
