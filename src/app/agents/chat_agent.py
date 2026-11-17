"""ReAct 对话 Agent：LangGraph create_react_agent + 数据中台 Tools + 多轮会话。

系统提示包含：
- 数据中台治理好的指标口径元数据（通过中台 API 获取并缓存，按 data_version 失效）；
- 取数优先、数字来自工具、缺数据说明的业务规则；
- HITL 规则：大额补货必须创建草稿单并明确告知"等待人工确认"。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
import uuid
import weakref

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from app.agents.grounding import EMPTY_REPLY_FALLBACK
from app.agents.prompt_loader import prompt_version, render_prompt
from app.agents.query_plan import build_query_plan, maybe_generative_rewrite, plan_brief
from app.agents.registry import AgentManifest, get_registry
from app.agents.tool_guard import (
    FAILURE_LIMIT as TOOL_FAILURE_LIMIT,
)
from app.agents.tool_guard import (
    REPEAT_LIMIT as TOOL_REPEAT_LIMIT,
)
from app.agents.tool_guard import (
    InvalidToolCallError,
    ToolLoopError,
    guard_summary,
    tool_guard_context,
    validate_tool_calls,
)
from app.auth import AuthUser
from app.config import get_settings
from app.db.engine import session_scope
from app.llm import (
    cache_hit_tokens,
    department_context,
    record_usage,
    turn_usage_context,
)
from app.memory import org_memory, session_memory, workspace
from app.memory.result_store import cited_result_ids
from app.memory.runtime import (
    current_chat_context,
    current_turn_log,
    emit_event,
    emit_stage,
    tool_context,
    turn_elapsed_ms,
    turn_log_context,
)
from app.observability import latency
from app.resilience import is_retryable
from app.security.scope import visible_categories
from app.tools.platform_tools import build_platform_tools, get_platform_client

logger = logging.getLogger("sku-agent.chat_agent")

# 工具名幻觉的纠错重试上限（复审 A3）：一次就够——第二次还错说明模型/提示本身有问题，
# 再试只是多花一次模型调用。该重试不占 `CHAT_MAX_RETRIES`，但受重试预算约束。
_INVALID_TOOL_RETRY_LIMIT = 1

# ---------------------------------------------------------------- 图步数上限（复审 R1）
# 一轮工具循环在图上有几个超步：`agent` → `post_model_hook` → `tools`。
# **hook 是独立节点**（`create_react_agent` 会把它插进图里），所以是 3 而不是 2。
# 早先按 2 算，导致"预算优先于步数上限"失效：budget=15 时第 11 次调用就撞
# `GraphRecursionError`，用户看到的错误码从 `TOOL_BUDGET_EXCEEDED` 变成了
# `AGENT_STEP_LIMIT`，`manifest.max_tool_calls` 也不再真正生效。
_SUPERSTEPS_PER_TOOL_LOOP = 3
_HAS_POST_MODEL_HOOK = True
# 余量：首轮 agent+hook、末轮直接作答（只有 agent+hook）、以及模型偶尔多绕一步。
_SUPERSTEP_SLACK = 6


def _recursion_limit_for(max_calls: int, *, hook: bool = _HAS_POST_MODEL_HOOK) -> int:
    """按"工具预算"换算图步数上限（保险绳），保证 **guard 的预算先触发**。

    `tests/test_tool_guard.py` 有一条真图用例（每次入参不同的循环）钉住这个顺序：
    先 `TOOL_BUDGET_EXCEEDED`，不许 `AGENT_STEP_LIMIT` 抢先。
    """
    steps = _SUPERSTEPS_PER_TOOL_LOOP if hook else 2
    return max(8, steps * max(1, int(max_calls)) + _SUPERSTEP_SLACK)


_METADATA_CACHE: dict[str, object] = {"version": None, "text": None, "at": 0.0, "error_at": 0.0}
_METADATA_TTL_SECONDS = 300.0        # 成功结果的内存缓存（进程内，避免每轮一次中台 RTT）
_METADATA_ERROR_TTL_SECONDS = 30.0   # 失败短路：中台挂了不要每轮都去撞（熔断之外再省一层）

# ---------------------------------------------------------------- 对话并发闸门
# 作用：限制「同时进行的对话轮次」，超出的排队，而不是把上游模型打成 429。
#
# 实现要点（比早先的 `dict[id(loop)] -> Semaphore` 更稳）：
# - 以**事件循环对象本身**做弱引用 key（`WeakKeyDictionary`）：loop 被回收时条目自动消失，
#   测试/脚本反复 `asyncio.run` 不会累积垃圾条目；
# - 维护真实 `active` 计数（而不是读 `Semaphore._value` 反推），`/api/health.limits.inflight` 才准确；
# - 配置的 `chat_max_concurrency` 变了且当前无活跃请求时，静默重建 semaphore。
_CHAT_GATES: "weakref.WeakKeyDictionary[object, _ChatGate]" = weakref.WeakKeyDictionary()


class TurnBudgetExceeded(RuntimeError):
    """本轮超过时间/重试预算。

    为什么必须有：重试与降级是**乘法**的（2 个模型 × (1+N) 次 × `LLM_TIMEOUT_SECONDS`），
    再加上出口溯源的重写调用，最坏情况一轮能占住一个并发槽十几分钟。宁可明确报错，
    也不让一次请求把整条通道堵死。

    `retryable=False`：重试只会把同一个预算再撞一次（重试逻辑在 `_invoke_agent` 里，
    预算耗尽时本就不该继续），这里显式声明，避免将来被 `is_retryable` 的文本规则误判。
    """

    retryable = False


class _ChatGate:
    __slots__ = ("limit", "semaphore", "active")

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self.semaphore = asyncio.Semaphore(self.limit)
        self.active = 0

    def ensure_limit(self, limit: int) -> None:
        """配置变更时重建（只在没有活跃请求时做，避免打断在跑的对话）。"""
        limit = max(1, int(limit))
        if limit != self.limit and self.active == 0:
            self.limit = limit
            self.semaphore = asyncio.Semaphore(limit)

    async def acquire(self) -> None:
        await self.semaphore.acquire()
        self.active += 1

    def release(self) -> None:
        self.active = max(0, self.active - 1)
        self.semaphore.release()


def _chat_gate() -> _ChatGate:
    loop = asyncio.get_running_loop()
    gate = _CHAT_GATES.get(loop)
    if gate is None:
        gate = _ChatGate(get_settings().chat_max_concurrency)
        _CHAT_GATES[loop] = gate
    else:
        gate.ensure_limit(get_settings().chat_max_concurrency)
    return gate


def reset_chat_semaphores() -> None:
    """测试/热更新配置用：清空所有事件循环的闸门。"""
    _CHAT_GATES.clear()


def chat_limits() -> dict:
    """给 /api/health 用：并发上限、当前在跑的轮次、每用户频次上限。

    注意「真实上游并发上限」≈ chat_max_concurrency + llm_max_concurrency：
    对话侧走自己的闸门（`_ChatGate`），巡检/研判/溯源重写走 `LLMClient` 的信号量，
    两套闸门互不知情。所以这里同时报出两边各自的在跑数与合计，避免把
    `chat_max_concurrency` 误读成"打给上游的总并发"。
    """
    settings = get_settings()
    from app.llm import get_llm

    inflight = sum(gate.active for gate in list(_CHAT_GATES.values()))
    llm_inflight = int(getattr(get_llm(), "inflight", 0) or 0)
    from app.observability import runtime_state

    # 会话锁后端：回答「同一会话并发被挡住了吗、靠的是进程内还是 Redis」
    try:
        from app.api.chat_routes import session_lock_state

        session_lock = session_lock_state()
    except Exception:  # noqa: BLE001 —— 循环导入/初始化期：健康检查不该因此失败
        session_lock = {"backend": "unknown", "required": None}

    return {
        "max_concurrency": max(1, int(settings.chat_max_concurrency)),
        "inflight": inflight,
        "llm_max_concurrency": max(1, int(settings.llm_max_concurrency)),
        "llm_inflight": llm_inflight,
        "total_inflight": inflight + llm_inflight,
        "active_streams": runtime_state.active_chat_streams(),
        "rate_limit_per_minute": int(settings.chat_rate_limit_per_minute),
        "global_qps": int(getattr(settings, "chat_global_qps", 0) or 0),
        # 工具循环守护（P1-1）：单轮成功调用预算 + 循环判定阈值（阈值按需求固定为 3）
        "tool_call_budget": int(getattr(settings, "chat_tool_call_budget", 15)),
        "tool_failure_limit": TOOL_FAILURE_LIMIT,
        "tool_repeat_limit": TOOL_REPEAT_LIMIT,
        "session_lock": session_lock,
        "note": "真实上游并发上限 ≈ max_concurrency + llm_max_concurrency（两套闸门互不知情）",
    }


def _load_memory_bundle(session_id: str, message: str,
                        scope: list[str] | None) -> dict:
    """同步读取本轮记忆（在工作线程里执行）：事实卡 + 工作集 + L3 档案引用。

    抽成独立同步函数的原因：SQLAlchemy 同步 Session 是阻塞调用，直接放在 async
    函数里会把事件循环卡住（SSE 心跳、其它会话都会被拖住），用 asyncio.to_thread 包起来。
    """
    with session_scope() as session:
        # 先记录「上一轮结束时」的记忆状态，用于展示本轮压缩/新增了什么
        prev = session_memory.get_memory_snapshot(session, session_id) or {}
        memory_previous = {
            "facts_version": prev.get("facts_version", 0),
            "working_version": prev.get("working_version", 0),
            "facts_card": (prev.get("facts_card") or "")[:3000],
            "trigger": prev.get("trigger") or "",
        }
        # 再把本轮输入并入工作集（指代解析要用），然后组装记忆上下文
        session_memory.refresh_memory(session, session_id, message)
        mem = session_memory.build_memory_context(session, session_id)
        snap = session_memory.get_memory_snapshot(session, session_id)
        working_set = (snap or {}).get("working_set") or {}
        memory_context = {
            "facts_card": (snap or {}).get("facts_card", "")[:3000],
            "working_set": session_memory.working_set_text(working_set),
            "facts_version": mem["facts_version"],
            "working_version": mem["working_version"],
            "trigger": (snap or {}).get("trigger") or "",
            "chars": len(mem["text"]),
        }
        # 复审 S1：引用解析返回 {"status","token","items"} —— 命中/无权限/不存在三态要能分开，
        # 否则模型只能拿一份"看起来像"的档案顶上。
        session_reference = org_memory.resolve_session_reference(session, message, scope)
        return {
            "memory_text": mem["text"],
            "memory_meta": {"facts_version": mem["facts_version"],
                            "working_version": mem["working_version"]},
            "memory_context": memory_context,
            "memory_previous": memory_previous,
            "working_set": working_set,
            "session_reference": session_reference,
        }


def _load_profile_text(username: str) -> str:
    """同步读取 L1 用户画像（工作线程里执行）。"""
    from app.memory import user_profile

    with session_scope() as session:
        return user_profile.profile_text(session, username)


def _save_query_plan(session_id: str, username: str, department: str | None,
                     plan: dict) -> dict | None:
    """同步落库查询计划并判断是否追问（工作线程里执行）。返回 clarify。"""
    clarify = None
    with session_scope() as session:
        asked = False
        if session_memory.should_ask_clarify(session, session_id, plan):
            clarify = plan.get("clarify")
            asked = clarify is not None
        session_memory.save_query_plan(
            session, session_id=session_id, username=username,
            department=department, plan=plan, asked_clarify=asked,
        )
    return clarify

FALLBACK_METADATA = (
    "可用库存 = 现存 − 锁定 − CRM 预订未发货 − 订单未发货占用；"
    "安全库存 = ceil(日均销量(近7天)×交期×0.3)；"
    "再订货点 = ceil(日均销量×(交期+3)) + 安全库存；"
    "预计售罄天数 = (可用库存 + 7天内到货在途) ÷ 日均销量(近7天)；"
    "动销率 = 近30天有销量SKU数 ÷ 有货SKU总数。"
)


async def _metadata_text() -> tuple[str, int | None]:
    """口径元数据：进程内 TTL 缓存 + 失败短路（每轮一次中台 RTT 其实很浪费）。

    - 命中 `_METADATA_TTL_SECONDS` 内的缓存 → 直接返回，不发请求；
    - 中台不可用时按 `_METADATA_ERROR_TTL_SECONDS` 短路（熔断之外再省一层），
      返回内置的 `FALLBACK_METADATA`（口径不能因为中台抖动就消失）。
    """
    now = time.monotonic()
    cached_text = _METADATA_CACHE.get("text")
    cached_version = _METADATA_CACHE.get("version")
    if cached_text and now - float(_METADATA_CACHE.get("at") or 0) < _METADATA_TTL_SECONDS:
        return str(cached_text), cached_version  # type: ignore[return-value]
    if now - float(_METADATA_CACHE.get("error_at") or 0) < _METADATA_ERROR_TTL_SECONDS:
        return FALLBACK_METADATA, None

    client = get_platform_client()
    data = await client.get("/api/v1/metadata/metrics")
    if "error" in data:
        _METADATA_CACHE["error_at"] = time.monotonic()
        return FALLBACK_METADATA, None
    version = data.get("data_version")
    if _METADATA_CACHE["version"] != version or not _METADATA_CACHE["text"]:
        _METADATA_CACHE["version"] = version
        _METADATA_CACHE["text"] = data.get("prompt_context") or FALLBACK_METADATA
    _METADATA_CACHE["at"] = time.monotonic()
    return str(_METADATA_CACHE["text"] or FALLBACK_METADATA), version


def _agent_tool_count(manifest: AgentManifest | None) -> int:
    """本轮该 Agent 实际可用工具数：与装配走同一条解析路径（不再另算一套）。

    历史坑：这里曾直接 `len(manifest.tool_names)`，而装配用的是 `resolve_tool_selection`。
    `tools: ["image"]`（空分组）在两边语义不同 → 日志写 0、实际装了全部 22 个。
    """
    from app.tools.platform_tools import expected_tool_count

    if manifest is None:
        return expected_tool_count(None)
    return expected_tool_count(list(manifest.tool_names))


def _role_text(user: AuthUser) -> str:
    return "管理员（可查看全部部门）" if user.is_admin else f"{user.department}部门用户（仅可访问本部门数据）"


def _org_scope(user: AuthUser) -> list[str] | None:
    """档案/记忆可见范围：统一走 `app.security.scope`（唯一实现）。"""
    return visible_categories(user)


def build_memory_sections(memory_text: str = "", plan: dict | None = None,
                          session_ref_text: str = "", profile_text: str = "") -> str:
    """动态记忆片段：L2 基线 + Skills、L1 偏好/事实卡/工作集、L3 档案引用、query_plan。

    L0 取数纪律（静态）不在这个函数里——它在提示词模板的静态区，
    这样"每轮会变"的内容只集中在模板尾部，前缀缓存才能稳定命中。
    """
    parts: list[str] = []
    baseline = workspace.load_workspace_baseline()
    if baseline:
        parts.append("## L2 工作区基线（AGENTS.md，长期有效）\n" + baseline)
    catalog = workspace.skills_catalog_text()
    if catalog:
        parts.append(catalog)
    if profile_text:
        parts.append(profile_text)
    # 注意：L0 取数纪律是**静态**规则，已上移到提示词模板的静态区（见 prompts/*.md），
    # 这里只保留"每轮会变"的内容（plan / 会话记忆 / L3 引用），以免破坏上下文缓存的前缀稳定性。
    if plan:
        parts.append(plan_brief(plan))
    if memory_text:
        parts.append(
            "## L1 会话记忆（事实卡 + 工作集，可直接复用但需复核 result_id）\n"
            "事实卡只是被压缩过的结论骨架，不是实时数据：只要用户问的指标/时间窗与事实卡不一致，"
            "或指标本身时效性强（可用库存、销量、在途、售罄天数），必须重新调用工具取数后作答，"
            "不得仅凭事实卡里的旧数字下结论；指代（这个款/它/刚才那个尺码）可以用 <working-set> 解析。\n"
            + memory_text
        )
    if session_ref_text:
        parts.append("## L3 组织记忆（@session 引用的历史结论脉络）\n" + session_ref_text)
    return "\n\n".join(parts)


async def build_system_prompt(user: AuthUser, *, memory_text: str = "", plan: dict | None = None,
                              session_ref_text: str = "", profile_text: str = "",
                              manifest: AgentManifest | None = None) -> tuple[str, int | None]:
    """渲染系统提示：模板由 manifest 指定（默认 `chat_system`），代码只负责填值。

    提示词外置的好处：能 review / diff / A/B；配合 `prompt_version`，事后能回答
    「这条结论是哪一版提示词产出的」。新增 Agent 只需要新增 md + json，不改这里。
    """
    prompt_name = manifest.prompt if manifest is not None else "chat_system"
    metadata, version = await _metadata_text()
    today = dt.date.today().isoformat()
    settings = get_settings()
    # memory_scope=none：不注入 L1 会话记忆 / L3 引用（用于"只按当前问题取数作答"的 Agent），
    # 但 L0 纪律、L2 基线与 query_plan 仍然保留（它们是规则与计划，不是会话记忆）。
    scope = (manifest.memory_scope if manifest is not None else "session") or "session"
    if scope == "none":
        memory_text = ""
        session_ref_text = ""
    memory_sections = build_memory_sections(memory_text, plan, session_ref_text, profile_text)
    text = render_prompt(
        prompt_name,
        today=today,
        username=user.username,
        role_text=_role_text(user),
        memory_sections=memory_sections,
        metadata=metadata,
        memory_max_inline_rows=settings.memory_max_inline_rows,
        replenish_qty_threshold=settings.replenish_qty_threshold,
        replenish_amount_threshold=f"{settings.replenish_amount_threshold:,.0f}",
    )
    return text, version


# ---------------------------------------------------------------- 共享 HTTP 连接池
# 为什么需要：对话侧每轮都会 new 一个 ChatOpenAI，而每个实例会自带一条连接池 ——
# 等于每轮都重新建连（丢 keep-alive）。这里复用一个 httpx.AsyncClient 给所有 ChatOpenAI 用，
# 每轮的 ChatOpenAI 仍然新建（per-call 的延迟回调依赖构造参数），但底层连接复用。
_HTTP_CLIENT: "httpx.AsyncClient | None" = None


def _shared_http_client() -> "httpx.AsyncClient":
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        import httpx

        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=float(get_settings().llm_timeout_seconds),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
    return _HTTP_CLIENT


async def aclose_shared_http() -> None:
    """应用关闭 / 测试重置：关掉共享连接池。"""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None and not _HTTP_CLIENT.is_closed:
        await _HTTP_CLIENT.aclose()
    _HTTP_CLIENT = None


def build_chat_agent(user: AuthUser, system_prompt: str,
                     manifest: AgentManifest | None = None):
    """按 manifest 装配 ReAct Agent：模型/温度/上限/工具集都可以逐 Agent 独立配置。

    manifest=None 时保持历史行为（全局模型 + 全量工具），保证"只有一个 Agent"时完全一致。
    """
    settings = get_settings()
    model_name = (manifest.model if manifest is not None and manifest.model else "") or settings.llm_model
    temperature = float(manifest.temperature) if manifest is not None else 0.2
    max_tokens = int(manifest.max_tokens) if manifest is not None else 2500
    allow = list(manifest.tool_names) if manifest is not None else None
    if allow == ["*"]:
        allow = None
    # 对话模型调用延迟采样：ChatOpenAI 不走 LLMClient，用回调把每次调用记进 latency.llm_chat。
    # sink / turn_log 显式传入（回调可能在线程池里跑，contextvars 不保证可用）：
    # 进度卡据此显示「模型调用 #k · 已 X.Xs」，运行详情据此把耗时归因到模型而不是工具。
    callbacks = []
    try:
        from app.memory.runtime import current_event_sink, current_turn_log
        from app.observability.llm_callback import LatencyCallbackHandler

        callbacks = [LatencyCallbackHandler(
            sink=current_event_sink(), turn_log=current_turn_log(),
        )]
    except Exception:  # noqa: BLE001 —— 采样失败不影响对话
        callbacks = []
    model = ChatOpenAI(
        model=model_name,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
        extra_body={"thinking": {"type": "disabled"}} if settings.llm_disable_thinking else None,
        callbacks=callbacks or None,
        http_async_client=_shared_http_client(),
    )
    tools = build_platform_tools(user, allow=allow)
    # 模型可能点到一个"别的 Agent 里有、当前 Agent 没有"的工具名（只读 Agent 没有写工具，
    # 这是最现实的幻觉形态）。LangGraph 的 ToolNode 对未知工具抛的是**空消息** RuntimeError，
    # 前端只会看到 `RuntimeError: ` 并因"非 fatal"自动重发一轮（复审 A3）。
    # 这里在模型产出之后、执行工具之前先校验：白名单直接取自实际装配结果，不另算清单。
    graph = create_react_agent(
        model, tools, prompt=SystemMessage(content=system_prompt),
        name=f"sku_chat_agent:{manifest.id}" if manifest is not None else "sku_chat_agent",
        post_model_hook=validate_tool_calls({tool.name for tool in tools}),
    )
    # 图级步数上限（P1-1 保险绳）：工具层守护拦「同参重复 / 连续失败 / 超预算」，这里拦
    # 「每次入参都不同、但模型一直在调工具」。
    # **真正的闸门是 tool_guard 的预算**，这条只是比它更晚触发的兜底——所以按图结构留足余量
    # （见 `_recursion_limit_for`：post_model_hook 是独立节点，一轮工具循环 3 个超步）。
    # 用 with_config 绑定而不是调用时传 config：调用点保持 `ainvoke({"messages": ...})`
    # 的既有形态（测试替身、脚本直调都不必跟着改签名）。
    max_calls = _tool_budget_of(manifest, settings)
    limit = _recursion_limit_for(max_calls, hook=_HAS_POST_MODEL_HOOK)
    bind = getattr(graph, "with_config", None)
    if bind is None:  # 测试替身/未来换实现：没有 with_config 就用框架默认上限
        return graph
    return bind({"recursion_limit": limit})


def _content_text(content) -> str:
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content or "")


# 结果需要「保留完整正文」的工具：知识库检索返回的是 SOP/规则原文，
# 出口溯源校验要靠它证明「答案里的阈值来自公司制度，不是模型编的」。
KB_TOOL_NAMES = frozenset({"search_knowledge"})


def _trace_result_limit(tool_name: str, settings=None) -> int:
    """工具结果在 trace 里保留多少字符。

    详单类工具只留 500 字（run_detail 会整份透出给前端，不能无限膨胀）；
    知识库结果按 `KB_EVIDENCE_MAX_CHARS` 保留——SOP 的阈值/天数常排在第三、四段，
    截掉就等于把「有依据的回答」判成「编造数字」。
    """
    if tool_name in KB_TOOL_NAMES:
        settings = settings or get_settings()
        return max(500, int(getattr(settings, "kb_evidence_max_chars", 6000)))
    return 500


async def _stream_agent(agent, messages: list) -> tuple[list, int]:
    """跑一轮 ReAct，并把「答案 token」边生成边推成 delta 事件；返回新增消息与 delta 数。

    为什么要把 messages 与 values 两种模式一起订阅：
    - messages：模型逐段输出（真流式，首字延迟从 7s 降到 1s 级）；
    - values：每步后的完整 state，用来拿工具轨迹与最终答复（messages 模式只有碎片）。
    带 tool_call_chunks 的碎片是「模型决定调工具」的过程，不是给用户看的答案，直接丢弃。

    图级 `recursion_limit` 在 `build_chat_agent` 里用 `with_config` 绑定（P1-1 保险绳），
    这里不需要再传 config。

    失败语义：已经推过 delta 再失败时，给异常打上 `partial_deltas`，上层据此**不重试**
    （重试会把半截答案重复推给前端）。
    """
    final_state: dict = {}
    deltas = 0
    # 复审 A4：按 AI 消息 id 记录「已推出去的字符数」。模型常见「先说一句再调工具」，
    # 那段文本不是最终答案——同一条消息随后出现工具调用时，通知前端撤回（delta_retract）。
    emitted: dict[str, int] = {}
    revoked: set[str] = set()
    try:
        async for mode, chunk in agent.astream(
            {"messages": messages}, stream_mode=["messages", "values"],
        ):
            if mode == "messages":
                piece, _meta = chunk
                msg_id = str(getattr(piece, "id", "") or "")
                # - tool_call_chunks 非空 = 模型正在决定调工具（过程，不是答案）；
                # - ToolMessage 等非 AI 消息是工具返回体，不能当 token 推给用户。
                if getattr(piece, "tool_call_chunks", None) or getattr(piece, "tool_calls", None):
                    if msg_id and emitted.get(msg_id) and msg_id not in revoked:
                        revoked.add(msg_id)
                        emit_event("delta_retract", {"reason": "tool_call",
                                                     "chars": int(emitted[msg_id])})
                    continue
                if getattr(piece, "type", "") != "ai" and piece.__class__.__name__ != "AIMessageChunk":
                    continue
                if msg_id in revoked:
                    continue
                text = _content_text(getattr(piece, "content", ""))
                if not text:
                    continue
                deltas += 1
                emitted[msg_id] = emitted.get(msg_id, 0) + len(text)
                emit_event("delta", {"text": text})
                continue
            if mode == "values" and isinstance(chunk, dict):
                final_state = chunk
    except Exception as exc:  # noqa: BLE001 —— 已推过 delta 就不重试，避免重复内容
        if deltas:
            exc.partial_deltas = deltas  # type: ignore[attr-defined]
        raise
    new_messages = list((final_state or {}).get("messages") or [])[len(messages):]
    return new_messages, deltas


# ---------------------------------------------------------------- Agent 解析（manifest 驱动）
# transient 判定统一在 `app.resilience.is_retryable`（类名 / HTTP 状态码 / 消息正则三级；
# 不含裸数字，避免把业务文案里的「500 件」误判成 HTTP 500）。
# 这里保留 `_is_transient` 这个名字作为别名：调用点与测试都用它，语义不变。
_is_transient = is_retryable


def resolve_agent(agent_id: str | None, message: str, plan: dict | None = None) -> dict:
    """决定本轮由哪个 Agent 执行：显式指定 > 关键词 > query_plan.intent > 默认。

    返回 {manifest, agent_id, routed_by, fallback_from}；被指定/路由到的 Agent 不可用时
    回落到默认 Agent，并把原因记进 run_detail（可观测，不静默降级）。
    """
    registry = get_registry()
    registry.maybe_reload()
    fallback_from = ""
    requested = (agent_id or "").strip()
    # 多 Agent 总开关：关掉后只走默认 Agent（显式 ?agent_id= 与关键词/intent 路由都不再生效），
    # 但原因仍记进 run_detail（可观测，不静默改语义）。
    if not getattr(get_settings(), "agents_enabled", True):
        default_manifest = registry.get(registry.default_id())
        default_id = default_manifest.id if default_manifest else ""
        return {"manifest": default_manifest, "agent_id": default_id,
                "routed_by": "agents_disabled",
                "fallback_from": requested if requested and requested != default_id else ""}
    if requested:
        manifest = registry.get(requested)
        if manifest is not None and manifest.enabled:
            return {"manifest": manifest, "agent_id": manifest.id,
                    "routed_by": "explicit", "fallback_from": ""}
        fallback_from = requested
    decision = registry.route(message, plan)
    manifest = registry.get(decision["agent_id"])
    if manifest is None or not manifest.enabled:
        default_manifest = registry.get(registry.default_id())
        return {"manifest": default_manifest,
                "agent_id": default_manifest.id if default_manifest else "",
                "routed_by": decision.get("routed_by", "default"),
                "fallback_from": fallback_from or decision.get("agent_id", "")}
    return {"manifest": manifest, "agent_id": manifest.id,
            "routed_by": decision.get("routed_by", "default"), "fallback_from": fallback_from}


async def run_chat(user: AuthUser, history: list[dict], message: str,
                   agent_id: str | None = None) -> dict:
    """执行一轮对话（非流式），并收集「本轮运行详情」（计划 / 工具步骤 / 记忆 / token / 权限）。

    运行详情用于看板在回答下方展示证据链：每个数字都能追溯到 result_id + as_of，
    事实卡展示「本轮注入」与「上一轮状态」的版本差异，体现上下文压缩。
    """
    return await _run_chat(user, history, message, token_stream=False, agent_id=agent_id)


async def run_chat_stream(user: AuthUser, history: list[dict], message: str,
                          agent_id: str | None = None) -> dict:
    """执行一轮对话（流式）：answer token 边生成边通过 `delta` 事件推给前端。

    与 run_chat 共用同一套记忆/计划/工具/溯源逻辑，唯一差别是 agent 用 `astream`：
    - `messages` 模式拿到模型逐段输出 → emit_event("delta")，前端先把气泡写出来；
    - `values` 模式拿到每步后的完整 state → 取最后一份作为工具轨迹与答复的来源。
    工具调用前的规划 token（tool_call_chunks）不推给前端，避免把「我要调工具」的
    中间推理当成答案显示。
    """
    return await _run_chat(user, history, message, token_stream=True, agent_id=agent_id)


def _tool_name_correction(exc: InvalidToolCallError) -> str:
    """工具调用不可执行时，追加到系统提示尾部的"纠正说明"（只影响本轮）。

    两种原因给不同纠正语：未知工具名 → 列可用清单；参数非法 → 要求合法 JSON。
    """
    allowed = "、".join(exc.allowed_tools[:20]) or "（无）"
    if getattr(exc, "reason", "") == "invalid_args":
        return (
            "\n\n## 工具参数纠正（系统自动追加，仅本轮有效）\n"
            f"你上次给工具 {('、'.join(exc.bad_tools[:5]) or '（未知）')} 的参数不是合法 JSON，"
            "本轮调用已中止。\n"
            "请重新给出**合法的 JSON 对象**作为参数（键名与工具参数说明一致，值类型正确）；"
            "如果不需要工具，就直接用文字作答。\n"
            f"本轮可用工具：{allowed}。"
        )
    return (
        "\n\n## 工具名纠正（系统自动追加，仅本轮有效）\n"
        f"你刚才调用了不存在的工具：{'、'.join(exc.bad_tools)}。\n"
        f"本轮**只有**这些工具可用：{allowed}。\n"
        "请严格使用上面的工具名重新作答；不要编造工具名，也不要把不存在的工具写进答案。"
    )


async def _invoke_agent(user: AuthUser, messages: list, agent, *,
                         token_stream: bool, manifest: AgentManifest | None,
                         system_prompt: str) -> tuple[list, int, int, str, bool, int]:
    """跑一次 Agent：transient 重试 → 仍失败切备用模型（各有限次），全程受重试预算约束。

    返回 (新增消息, delta 数, 重试次数, 实际使用的模型, 是否用了备用模型, 工具名纠错次数)。

    硬规则：
    1. **已经吐出 delta 的失败不重试、也不切备用模型**——否则前端会收到两段答案；
    2. **非瞬时错误（业务/参数/鉴权）直接上抛**：换一个模型也一样会失败，
       白白多打一次上游、可能再等满一个 `llm_timeout_seconds`；
    3. 备用模型只在「主模型可重试失败且重试额度用完」后才启用；
    4. **重试预算**：`CHAT_RETRY_BUDGET_SECONDS` 用尽后不再重试、也不再降级，
       直接抛 `TurnBudgetExceeded`（否则 2 模型 × N 次 × 超时 会把并发槽占满十几分钟）；
    5. **工具名幻觉**（复审 A3）：先做一次"把可用工具名写进提示"的纠错重试（多数能自救），
       仍失败才上抛（路由层转 422 + fatal，前端不自动重发）。
    """
    settings = get_settings()
    max_retries = max(0, int(getattr(settings, "chat_max_retries", 0)))
    fallback_model = (getattr(settings, "chat_fallback_model", "") or "").strip()
    retry_budget = max(0.0, float(getattr(settings, "chat_retry_budget_seconds", 90.0)))
    started = time.monotonic()

    attempts: list[AgentManifest | None] = [manifest]
    if fallback_model and manifest is not None:
        clone = manifest.model_copy(deep=True)
        clone.model = fallback_model
        attempts.append(clone)

    retries = 0
    invalid_tool_retries = 0
    correction = ""
    last_exc: BaseException | None = None

    def _rebuild(attempt_manifest: AgentManifest | None):
        return build_chat_agent(user, system_prompt + correction, attempt_manifest)

    for idx, attempt_manifest in enumerate(attempts):
        # 预算同时约束「重试」和「降级」：慢主模型跑完再换备用模型，等于把预算翻倍
        if (idx > 0 or retries) and time.monotonic() - started >= retry_budget:
            raise TurnBudgetExceeded(
                f"本轮重试/降级预算 {retry_budget:.0f}s 已用尽，已中止（最后错误：{last_exc}）"
            )
        model_used = (
            (attempt_manifest.model if attempt_manifest is not None and attempt_manifest.model else "")
            or settings.llm_model
        )
        active = agent if idx == 0 else _rebuild(attempt_manifest)
        attempt = 0
        while True:
            try:
                if token_stream:
                    new_messages, deltas = await _stream_agent(active, messages)
                else:
                    result = await active.ainvoke({"messages": messages})
                    new_messages, deltas = result["messages"][len(messages):], 0
                return (new_messages, deltas, retries, model_used, idx > 0,
                        invalid_tool_retries)
            except Exception as exc:  # noqa: BLE001 —— 只重试 transient
                last_exc = exc
                # 复审 R5：**所有**上抛路径都挂一份守护快照。早先只给「循环/步数」挂，
                # 于是工具名纠错失败、预算用尽这类中止轮次在台账里 `tool_calls=0`
                # （明明已经调过几次工具），trajectory 的中止轮工具数偏低。
                if not getattr(exc, "guard", None):
                    try:
                        exc.guard = guard_summary() or {}  # type: ignore[attr-defined]
                    except Exception:  # noqa: BLE001
                        pass
                if isinstance(exc, ToolLoopError) or _is_graph_recursion(exc):
                    # 循环守护/步数上限：重试只会把同一个循环再跑一遍。
                    raise
                if getattr(exc, "partial_deltas", 0):
                    # 已经推给前端的半截答案无法撤回：直接上抛，不重试也不降级
                    raise
                if isinstance(exc, InvalidToolCallError):
                    # 工具名/参数不合法：一次纠错重试（把可用工具名/参数要求写进系统提示），
                    # 不占 max_retries
                    if invalid_tool_retries >= _INVALID_TOOL_RETRY_LIMIT:
                        raise
                    if time.monotonic() - started >= retry_budget:
                        raise TurnBudgetExceeded(
                            f"本轮重试预算 {retry_budget:.0f}s 已用尽，已中止（最后错误：{exc}）"
                        ) from exc
                    invalid_tool_retries += 1
                    correction = _tool_name_correction(exc)
                    logger.warning("模型工具调用不可执行（%s），已纠错重试（第 %s 次）：%s",
                                   getattr(exc, "reason", ""), invalid_tool_retries,
                                   exc.bad_tools)
                    active = _rebuild(attempt_manifest)
                    continue
                if not _is_transient(exc):
                    # 业务/参数/鉴权类错误：换模型也一样失败，直接上抛
                    raise
                if attempt >= max_retries:
                    break            # 主模型重试额度用完 → 才允许进 fallback
                if time.monotonic() - started >= retry_budget:
                    raise TurnBudgetExceeded(
                        f"本轮重试预算 {retry_budget:.0f}s 已用尽，已中止（最后错误：{exc}）"
                    ) from exc
                retries += 1
                attempt += 1
                await asyncio.sleep(0.8 * attempt)
    raise last_exc if last_exc is not None else RuntimeError("agent 调用失败")


def _tool_budget_of(manifest: AgentManifest | None, settings) -> int:  # noqa: ANN001
    """单轮允许的成功工具调用次数：manifest 优先，缺省用全局预算。"""
    configured = int(getattr(manifest, "max_tool_calls", 0) or 0) if manifest is not None else 0
    return max(1, configured or int(getattr(settings, "chat_tool_call_budget", 15)))


def _is_graph_recursion(exc: BaseException) -> bool:
    """langgraph 的步数上限异常（`GraphRecursionError`，按类名判断以免强依赖内部路径）。"""
    return type(exc).__name__ == "GraphRecursionError"


def _attach_trace(exc: BaseException) -> None:
    """把本轮追踪标记挂到异常上（P1-2：出 turn 上下文之前必须记住它）。"""
    if getattr(exc, "trace_id", ""):
        return
    try:
        from app.observability import trace as trace_mod

        exc.trace_id = str(trace_mod.current_trace_id() or "-")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 —— 观测失败不影响错误上抛
        pass


async def _run_chat(user: AuthUser, history: list[dict], message: str,
                    *, token_stream: bool, agent_id: str | None = None) -> dict:
    turn = current_chat_context()
    session_id = turn.get("session_id") or ""
    include_query_plan = bool(turn.get("include_query_plan"))
    turn_id = "t_" + uuid.uuid4().hex[:10]

    with turn_log_context(turn_id, session_id), turn_usage_context() as usage:
        from app.observability.otel import span

        # 单轮硬超时：预算类配置（重试/降级/溯源重写）叠加后最坏情况可能十几分钟，
        # 这里兜住整轮，超时给出明确错误而不是让连接一直挂着。
        turn_timeout = float(getattr(get_settings(), "chat_turn_timeout_seconds", 300.0))
        # 注意 try 的位置（复审 D2）：`except TimeoutError` 里 raise 的 TurnBudgetExceeded
        # 不会被同一个 try 的兄弟 except 捕获，所以 trace 补丁必须放在**外层**——
        # 否则硬超时这条最需要追踪标记的路径，反而只会给前端一个「追踪 -」。
        try:
            with span("chat.turn", turn_id=turn_id, session_id=session_id,
                      username=user.username):
                try:
                    async with asyncio.timeout(turn_timeout):
                        body = await _run_chat_body(user, history, message,
                                                    token_stream=token_stream, agent_id=agent_id)
                except TimeoutError as exc:
                    # `asyncio.timeout` 抛的 TimeoutError 一定带 `from <CancelledError>`；
                    # 业务代码自己抛的 TimeoutError（例如流式中断）没有这层 cause —— 原样上抛。
                    if not isinstance(exc.__cause__, asyncio.CancelledError):
                        raise
                    raise TurnBudgetExceeded(
                        f"本轮超过 {turn_timeout:.0f}s 硬超时已中止：可重试，或把问题拆小（单 SKU / 单指标）"
                    ) from exc
        except (ToolLoopError, TurnBudgetExceeded) as exc:
            # 预算/循环类错误要带「追踪标记」给前端：此刻仍在 turn 上下文里，
            # 出了这个 with 就拿不到 turn_id 了，所以在这里补到异常上（P1-2 / D2）。
            _attach_trace(exc)
            raise
        except Exception as exc:  # noqa: BLE001 —— 所有异常都补追踪标记（N6：流式中途失败也要能对账）
            _attach_trace(exc)
            raise
        detail = _build_run_detail(turn_id, body, usage, user)
        latency.record("chat_turn", detail.get("elapsed_ms") or 0)

    plan = body.get("plan")
    return {
        "reply": body["reply"],
        "tool_trace": body["tool_trace"],
        "metadata_version": body["metadata_version"],
        "query_plan": plan if include_query_plan else None,
        "clarify": body.get("clarify"),
        "cited_result_ids": cited_result_ids(body["reply"]),
        "memory": body.get("memory_meta") or {"facts_version": 0, "working_version": 0},
        "session_references": body.get("session_references") or [],
        "run_detail": detail,
        "streamed": bool(body.get("streamed_deltas")),
        "streamed_unverified": bool(body.get("streamed_unverified")),
        # 复审 R3：模型未给出正文时前端/评测都能看到（不是"看起来成功的一轮"）
        "reply_empty": bool(body.get("reply_empty")),
        "agent": body.get("agent") or {},
    }


def _build_timeline(stages: list[dict], llm_calls: list[dict], steps: list[dict]) -> list[dict]:
    """按发生顺序合并「阶段 / 模型调用 / 工具调用」，供前端画本轮时间轴。

    这是回答"为什么工具只有 16ms，界面却等了好几秒"的关键：把模型调用单独成行，
    中间的秒数就归到模型上，而不是被误认为是某个工具在慢慢跑。
    """
    events: list[dict] = []
    for mark in stages:
        events.append({
            "kind": "stage",
            "offset_ms": int(mark.get("offset_ms") or 0),
            "label": mark.get("label") or mark.get("stage") or "",
            "stage": mark.get("stage") or "",
            "elapsed_ms": None,
        })
    for call in llm_calls:
        kind = call.get("kind") or ""
        events.append({
            "kind": "llm",
            "offset_ms": int(call.get("start_offset_ms") or 0),
            "label": f"模型调用 #{call.get('index') or '?'}",
            "kind_label": {"tool_call": "决定取数", "answer": "生成答复"}.get(kind, ""),
            "elapsed_ms": int(call.get("elapsed_ms") or 0),
            "input_tokens": int(call.get("input_tokens") or 0),
            "output_tokens": int(call.get("output_tokens") or 0),
        })
    for step in steps:
        events.append({
            "kind": "tool",
            "offset_ms": int(step.get("start_offset_ms") or 0),
            "label": step.get("tool") or "",
            "elapsed_ms": int(step.get("elapsed_ms") or 0),
            "result_id": step.get("result_id") or "",
            "as_of": step.get("as_of") or "",
            "agent_cached": bool(step.get("agent_cached")),
            "truncated": bool(step.get("truncated")),
            "row_count": int(step.get("row_count") or 0),
            "status": step.get("status") or "ok",
        })
    order = {"stage": 0, "llm": 1, "tool": 2}
    events.sort(key=lambda e: (e["offset_ms"], order.get(e["kind"], 9)))
    return events


def _grounding_detail(body: dict) -> dict:
    """出口闸门报告 + 「用户看到的初稿是否已经流出去」这个事实。

    流式和出口闸门天生有张力：token 先推给用户，校验在最后才跑。与其假装没有这件事，
    不如显式标注 `streamed_before_check`，让前端能如实提示「刚才那版是未校验初稿」。
    """
    report = dict(body.get("grounding") or {"status": "disabled"})
    report["streamed_before_check"] = bool(body.get("streamed_deltas")) and report.get(
        "status"
    ) in ("repaired", "degraded", "error")
    return report


def _build_run_detail(turn_id: str, body: dict, usage: dict, user: AuthUser) -> dict:
    """把本轮采集到的步骤/用量/记忆快照组装成前端可直接渲染的 run_detail。"""
    state = current_turn_log() or {}
    steps = list(state.get("steps") or [])
    llm_calls = list(state.get("llm_calls") or [])
    stages = list(state.get("stages") or [])
    total_ms = turn_elapsed_ms()
    llm_ms = sum(int(c.get("elapsed_ms") or 0) for c in llm_calls)
    tools_ms = sum(int(s.get("elapsed_ms") or 0) for s in steps)
    timeline = _build_timeline(stages, llm_calls, steps)
    plan = body.get("plan") or {}
    settings = get_settings()
    context = body.get("memory_context") or {}
    previous = body.get("memory_previous") or {}
    refs = body.get("session_references") or []
    allowed = (plan.get("allowed_departments") if plan else None)
    local_usage = body.get("llm_usage") or {}
    agent_info = body.get("agent") or {}
    # 归属要诚实：per-agent 模型 / 回退模型都可能与全局默认不同
    model_used = agent_info.get("model_used") or settings.llm_model
    model_requested = agent_info.get("model_requested") or settings.llm_model
    # 提示词版本同理：diagnostician/operator 用的是各自的 md，
    # 早先这里写死 prompt_version("chat_system")，导致"结论由哪版提示词产出"这件事记错。
    prompt_name = agent_info.get("prompt") or "chat_system"
    prompt_used_version = agent_info.get("prompt_version") or prompt_version(prompt_name)
    # 复审 R5：对话侧 token 优先用**回调**采集的口径。
    # 原因：`local_usage` 是从"最终消息"里汇总的，纠错/重试那几次失败的调用不进消息，
    # 于是它们的 token 与次数被漏掉（成本偏乐观）。回调（`llm_calls`）按调用记账，
    # 天然包含重试轮；拿不到 token（回调没给）时回退到消息汇总，并显式标注来源。
    msg_chat_calls = int(local_usage.get("calls") or 0)
    cb_calls = len(llm_calls)
    cb_in = sum(int(c.get("input_tokens") or 0) for c in llm_calls)
    cb_out = sum(int(c.get("output_tokens") or 0) for c in llm_calls)
    chat_calls = max(cb_calls, msg_chat_calls)
    if cb_in or cb_out:
        chat_in, chat_out = cb_in, cb_out
        token_source = "callback"
    else:
        chat_in = int(local_usage.get("input_tokens") or 0)
        chat_out = int(local_usage.get("output_tokens") or 0)
        token_source = "messages"
    llm = {
        "model": model_used,                 # 实际使用的模型（看板「模型」一栏）
        "model_requested": model_requested,  # 请求的模型（回退时才与 model 不同）
        "model_fallback_used": bool(agent_info.get("fallback_used")),
        "calls": chat_calls + int(usage.get("calls") or 0),
        "input_tokens": chat_in + int(usage.get("input_tokens") or 0),
        "output_tokens": chat_out + int(usage.get("output_tokens") or 0),
        "errors": int(usage.get("errors") or 0),
        # 比"进了最终消息的调用"多出来的调用次数：纠错重试 / 备用模型重试等（0 = 没有）。
        # 这些调用的 token 早先被漏掉，成本口径偏乐观；现在由回调口径补上。
        "retry_calls": max(0, chat_calls - msg_chat_calls),
        "token_source": token_source,
        # 每次调用的耗时/用途（拿不到 token 记为 0，不臆造数字）
        "calls_detail": llm_calls,
    }
    # 本轮成本估算（元）：按 LLM_PRICE_INPUT/OUTPUT 单价算，回答"哪条链路在烧钱"
    llm["cost_cny"] = round(
        (llm["input_tokens"] / 1_000_000) * float(settings.llm_price_input or 0.0)
        + (llm["output_tokens"] / 1_000_000) * float(settings.llm_price_output or 0.0),
        6,
    )
    # 上下文缓存（DeepSeek 自动前缀缓存）：命中 token 与命中率，成本解释用
    cache_hit = int(local_usage.get("cache_hit_tokens") or 0) + int(
        usage.get("cache_hit_tokens") or 0
    )
    llm["prompt_cache"] = {
        "hit_tokens": cache_hit,
        "input_tokens": llm["input_tokens"],
        "hit_rate": round(cache_hit / llm["input_tokens"], 4) if llm["input_tokens"] else 0.0,
        "note": "命中部分按更低单价计费；提示前缀稳定 + 顺序固定（静态在前、动态在后）命中率更高",
    }
    return {
        "turn_id": turn_id,
        "as_of": dt.datetime.now().isoformat(timespec="seconds"),
        "elapsed_ms": total_ms,        # 可复现性：这条结论由哪一版提示词 + 哪一版口径产出。
        # name/version 是**权威字段**（本轮真正渲染的那份 md，per-agent 各不相同）；
        # chat_system 保留为兼容字段，便于对比"默认模板"的版本。
        "prompt": {
            "name": prompt_name,
            "version": prompt_used_version,
            "chat_system": prompt_version("chat_system"),
            "metric_version": (plan or {}).get("metric_version") or settings.metric_version,
        },
        "timeline": timeline,
        "phases": {
            "llm_ms": llm_ms,
            "tools_ms": tools_ms,
            "other_ms": max(0, total_ms - llm_ms - tools_ms),
            "total_ms": total_ms,
            "llm_calls": len(llm_calls),
            "tool_calls": len(steps),
        },
        "llm": llm,
        "plan": {
            "plan_id": plan.get("plan_id"),
            "intent": plan.get("intent"),
            "confidence": plan.get("confidence"),
            "entities": plan.get("entities"),
            "time_range": plan.get("time_range"),
            "required_data": plan.get("required_data"),
            "retrieval_keywords": (plan.get("retrieval_query") or {}).get("keywords"),
            "assumptions": plan.get("assumptions"),
            "metric_version": plan.get("metric_version"),
            "resolved_from": plan.get("resolved_from"),
            "blocked": plan.get("blocked"),
            # 「该不该追问」要能在 run_detail 里看到：离线/线上评测都靠它判定
            # （`answer_eval` 的 clarify 维度，以及"口径解释类问题不该追问 SKU"这类回归）。
            "needs_clarify": plan.get("needs_clarify"),
            "clarify": plan.get("clarify"),
        } if plan else None,
        "steps": steps,
        # 工具循环守护（P1-1）：本轮成功调用次数/是否被判定循环而中止。
        # 值由 `_run_chat_body` 在**退出守护上下文之前**快照（`body["guard"]`）——
        # 这里再调 `guard_summary()` 只会拿到 None（上下文已退出），复审 E4 就是这条。
        "guard": body.get("guard") or guard_summary(),
        "agent": body.get("agent") or {},
        # 复审 R3：这一轮到底"答没答"。`is_fallback=True` 表示模型没给正文、用了兜底文案——
        # trajectory 据此把该轮排除出"成功"，answer_eval 据此判 fail，前端可据此提示。
        "answer": {
            "is_fallback": bool(body.get("reply_empty")),
            "chars": len(str(body.get("reply") or "")),
            "fallback_text": EMPTY_REPLY_FALLBACK if body.get("reply_empty") else "",
        },
        "grounding": _grounding_detail(body),
        "memory": {
            "context": context,
            "previous": previous,
            "facts_version": context.get("facts_version", 0),
            "working_version": context.get("working_version", 0),
            "facts_delta": (context.get("facts_version", 0) or 0)
            - (previous.get("facts_version", 0) or 0),
        },
        "references": {
            "result_ids": [s.get("result_id") for s in steps if s.get("result_id")],
            "artifacts": [s.get("artifact_path") for s in steps if s.get("artifact_path")],
            "analyses": refs,
            # 复审 S1：`@session` 引用解析的结果（none/ok/denied/not_found + token + 命中条数）。
            # 失败也留痕——否则审计只能看到"这轮没引用任何档案"，看不出用户引用过、且没找到。
            "session_reference": body.get("session_reference")
                                    or {"status": "none", "token": "", "count": 0},
        },
        "permission": {
            "department": "管理员" if user.is_admin else (user.department or "未知"),
            "allowed": allowed if allowed is not None else visible_categories(user),
            "denied_steps": [s.get("tool") for s in steps if s.get("status") == "error"],
        },
    }


async def _run_chat_body(user: AuthUser, history: list[dict], message: str,
                         *, token_stream: bool = False, agent_id: str | None = None) -> dict:
    """执行一轮对话：history 为上一轮消息（role/content 字典），message 为新输入。

    四层记忆在这里汇合：
    - L1 会话记忆：读 `<analysis-facts>` + `<working-set>`，本轮结束后回写；
    - L2 工作区记忆：AGENTS.md 基线 + Skills 目录常驻系统提示；
    - L3 组织记忆：`@session` 引用只注入结论脉络，跨会话复用靠 search_analyses 工具；
    - query_plan：对话前做结构化解析（确定性优先），默认不改写用户原句。
    """
    settings = get_settings()
    turn = current_chat_context()
    session_id = turn.get("session_id") or ""

    memory_text = ""
    session_ref_text = ""
    working_set: dict = {}
    memory_meta = {"facts_version": 0, "working_version": 0}
    memory_context: dict = {}
    memory_previous: dict = {}
    # 复审 S1：引用解析的完整结果（status/token/items），run_detail 要能回答"这轮引用了什么、成不成功"
    session_reference: dict = {"status": "none", "token": "", "items": []}
    refs: list[dict] = []
    emit_stage("memory", "读取会话记忆（事实卡 + 工作集）")
    if session_id:
        # 记忆读写是同步 SQLAlchemy 调用：放到线程里跑，别阻塞事件循环
        # （否则同一进程里的 SSE 心跳、其它会话的取数都会被一条 5ms 的查询顶住）
        bundle = await asyncio.to_thread(_load_memory_bundle, session_id, message, _org_scope(user))
        memory_text = bundle["memory_text"]
        memory_meta = bundle["memory_meta"]
        memory_context = bundle["memory_context"]
        memory_previous = bundle["memory_previous"]
        session_reference = bundle.get("session_reference") or session_reference
        refs = list(session_reference.get("items") or [])
        # 工作集必须回填给 query_plan：否则「这个款/它/刚才那个尺码」解析不到实体，
        # confidence 掉到阈值以下还会多余追问一次（早先这里漏了赋值，bundle 算完就被丢掉）
        working_set = bundle.get("working_set") or {}
        session_ref_text = org_memory.analysis_context(session_reference)

    plan: dict | None = None
    if settings.query_plan_enabled:
        emit_stage("plan", "解析查询计划（意图/实体/时间窗）")
        plan = build_query_plan(message, user=user, working_set=working_set)
        if settings.query_rewrite_llm:
            plan = await maybe_generative_rewrite(plan, message)
        emit_stage("plan_done", "查询计划已生成",
                   plan_id=(plan or {}).get("plan_id"), intent=(plan or {}).get("intent"),
                   confidence=(plan or {}).get("confidence"))

    profile_text = ""
    try:
        profile_text = await asyncio.to_thread(_load_profile_text, user.username)
    except Exception:  # noqa: BLE001 —— 画像读取失败不影响对话
        profile_text = ""

    # 决定本轮由哪个 Agent 执行（显式 agent_id > 关键词 > plan.intent > 默认）
    agent_info = resolve_agent(agent_id, message, plan)
    manifest = agent_info.get("manifest")
    if manifest is None:  # 注册表为空/全禁用：退回"无 manifest"的历史行为，保证服务可用
        agent_info = {"agent_id": "analyst", "routed_by": "fallback", "fallback_from": ""}
    emit_stage("route", f"路由到 Agent：{agent_info['agent_id']}（{agent_info['routed_by']}）",
               agent=agent_info["agent_id"], routed_by=agent_info["routed_by"])

    system_prompt, metadata_version = await build_system_prompt(
        user, memory_text=memory_text, plan=plan, session_ref_text=session_ref_text,
        profile_text=profile_text, manifest=manifest,
    )
    agent = build_chat_agent(user, system_prompt, manifest)
    emit_stage("agent", "调用 DeepSeek（ReAct 工具循环）")
    messages = []
    for item in history:
        if item.get("role") == "user":
            messages.append(HumanMessage(content=item.get("content", "")))
        else:
            messages.append(AIMessage(content=item.get("content", "")))
    messages.append(HumanMessage(content=message))

    # 对话产生的 LLM 调用按用户部门归因（管理员归入「管理员」）
    dept = "管理员" if user.is_admin else (user.department or "未知")
    # 守护状态在 with 之外组装 run_detail：由 body 透传（P1-1 可观测）
    guard_info: dict | None = None
    deltas = 0
    retries = 0
    model_used = ""
    fallback_used = False
    invalid_tool_retries = 0
    wait_started = time.perf_counter()
    gate = _chat_gate()
    await gate.acquire()
    latency.record("chat_wait", (time.perf_counter() - wait_started) * 1000)
    # 闸门覆盖「整轮」，而不是只包住模型调用：出口溯源校验（grounding）会为了重写再调一次
    # 模型，早先它发生在 gate.release() 之后 —— 那次调用完全不受 chat_max_concurrency 约束，
    # 于是"真实并发上限"其实是两套闸门之和（见 /api/health.limits.total_inflight）。
    # tool_guard_context（P1-1）只包住 ReAct 工具循环：同工具同参反复调用 / 连续失败 /
    # 超成功预算 → 立即终止本轮；溯源重写那次模型调用不涉及工具，不需要计数。
    tool_budget = _tool_budget_of(manifest, get_settings())
    try:
        with tool_context(session_id=session_id or None, username=user.username,
                          department=user.department,
                          agent_id=agent_info["agent_id"]), department_context(dept), \
                tool_guard_context(tool_budget):
            new_messages, deltas, retries, model_used, fallback_used, \
                invalid_tool_retries = await _invoke_agent(
                    user, messages, agent, token_stream=token_stream, manifest=manifest,
                    system_prompt=system_prompt,
                )
            # 守护状态在退出上下文前快照一份：run_detail 在 with 之外组装（P1-1 可观测）
            guard_info = guard_summary()
        # 注意文案：真正的「生成答复」发生在上面的 agent.ainvoke 里（模型最后一次调用），
        # 这里只是本地汇总，改成实话，避免把「答复阶段」误读成耗时大头。
        emit_stage("answer", "整理答复（本地汇总证据链，无模型调用）")
        reply = ""
        reply_empty = False      # 复审 R3：模型没给正文（含参数非法导致图直接结束）
        tool_trace: list[dict] = []
        # 对话用的 ChatOpenAI 直接由 agent 创建，token 用量从每条 AIMessage 的 usage_metadata 取
        llm_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_hit_tokens": 0}
        for m in new_messages:
            if isinstance(m, AIMessage):
                usage_meta = getattr(m, "usage_metadata", None) or {}
                in_tokens = int(usage_meta.get("input_tokens") or 0)
                out_tokens = int(usage_meta.get("output_tokens") or 0)
                if in_tokens or out_tokens:
                    llm_usage["calls"] += 1
                    llm_usage["input_tokens"] += in_tokens
                    llm_usage["output_tokens"] += out_tokens
                    # 上下文缓存命中（DeepSeek 自动前缀缓存）：命中率越高越省钱，
                    # 也解释了「同样的提示多轮问，成本为什么在降」
                    llm_usage["cache_hit_tokens"] += cache_hit_tokens(m)
                if m.tool_calls:
                    for tc in m.tool_calls:
                        tool_trace.append({"type": "call", "tool": tc.get("name"),
                                           "args": tc.get("args", {})})
                elif _content_text(m.content):
                    reply = _content_text(m.content)
            elif m.__class__.__name__ == "ToolMessage":
                content = _content_text(getattr(m, "content", ""))
                tool_name = getattr(m, "name", "")
                tool_trace.append({"type": "result", "tool": tool_name,
                                   "result": content[:_trace_result_limit(tool_name, settings)]})
        if not reply:
            # 复审 R3：模型没给正文（含"工具参数非法导致图直接结束"的情形）。
            # 打上标记，避免这一轮在 trajectory 里被当成"成功"、在评测里被当成"没数字所以没问题"。
            reply = EMPTY_REPLY_FALLBACK
            reply_empty = True

        # 出口闸门：数字必须能追溯到本轮工具结果（确定性规则 → 一次重写 → 降级留痕）
        # 空答复没有数字可校验，直接标 skipped 并留痕（不重写、不降级，也不假装通过）。
        grounding: dict = {"status": "disabled", "issues": []}
        if settings.grounding_enabled and reply_empty:
            grounding = {"status": "skipped", "issues": [],
                         "note": "模型未给出正文（兜底文案），无可校验数字"}
        elif settings.grounding_enabled:
            emit_stage("grounding", "校验答案数字与本轮取数结果的一致性")
            try:
                from app.agents.grounding import ground_reply

                grounded = await ground_reply(
                    reply, steps=list((current_turn_log() or {}).get("steps") or []),
                    tool_trace=tool_trace,
                )
                reply = grounded["reply"]
                grounding = grounded["report"]
                latency.record("grounding", grounding.get("elapsed_ms") or 0)
            except Exception as exc:  # noqa: BLE001 —— 校验器故障不能吞掉回答
                grounding = {"status": "error", "issues": [], "note": str(exc)[:200]}
            emit_stage("grounding_done", f"溯源校验：{grounding.get('status')}",
                       status=grounding.get("status"))

        clarify = None
        if session_id and plan:
            clarify = await asyncio.to_thread(
                _save_query_plan, session_id, user.username, user.department, plan,
            )
        if clarify is None and plan and plan.get("needs_clarify"):
            # 没落库场景（如脚本直调 run_chat）也保持「最多追问一次」的对外语义
            clarify = plan.get("clarify")
        # 对话用的 ChatOpenAI 不经过 LLMClient，这里把用量补登记到全局统计（看板 LLM 卡片/来源/部门）
        if llm_usage.get("calls") or llm_usage.get("input_tokens") or llm_usage.get("output_tokens"):
            record_usage(
                calls=int(llm_usage.get("calls") or 0),
                input_tokens=int(llm_usage.get("input_tokens") or 0),
                output_tokens=int(llm_usage.get("output_tokens") or 0),
                department="管理员" if user.is_admin else (user.department or "未知"),
                source="chat",
            )
    finally:
        gate.release()
    return {
        "reply": reply,
        "tool_trace": tool_trace,
        "metadata_version": metadata_version,
        "plan": plan,
        "clarify": clarify,
        "memory_meta": memory_meta,
        "memory_context": memory_context,
        "memory_previous": memory_previous,
        "session_references": [r.get("analysis_id") for r in refs],
        # 复审 S1：引用解析状态（none/ok/denied/not_found）+ 原始 token
        "session_reference": {"status": session_reference.get("status") or "none",
                              "token": session_reference.get("token") or "",
                              "count": len(refs)},
        "llm_usage": llm_usage,
        "grounding": grounding,
        "guard": guard_info,
        # 复审 R3：模型这轮有没有真的给出正文（False = 已用兜底文案）
        "reply_empty": reply_empty,
        "streamed_deltas": deltas,
        # 流式先把「初稿」推给用户，出口闸门之后才可能改写/标记降级：
        # 这个字段让前端能如实提示「你刚才看到的是未校验的初稿」
        "streamed_unverified": bool(deltas) and grounding.get("status") in (
            "repaired", "degraded", "error",
        ),
        "agent": {
            "id": agent_info.get("agent_id") or "",
            "name": manifest.name if manifest is not None else "",
            "version": manifest.version if manifest is not None else "",
            "prompt": manifest.prompt if manifest is not None else "chat_system",
            "prompt_version": manifest.prompt_version if manifest is not None else "",
            # 归因要诚实：请求的模型 / 实际使用的模型 / 是否动用了备用模型
            "model_requested": (manifest.model if manifest is not None and manifest.model
                                else settings.llm_model),
            "model_used": model_used or settings.llm_model,
            "model": model_used or settings.llm_model,
            "fallback_used": bool(fallback_used),
            "tools": list(manifest.tool_names) if manifest is not None else ["*"],
            "tool_count": _agent_tool_count(manifest),
            "tools_all": bool(manifest is not None and manifest.tool_names == ["*"]),
            "memory_scope": (manifest.memory_scope if manifest is not None else "session"),
            "max_reflection_rounds": (manifest.max_reflection_rounds if manifest is not None else 0),
            "routed_by": agent_info.get("routed_by") or "default",
            "fallback_from": agent_info.get("fallback_from") or "",
            "retries": retries,
            # 复审 A3：本轮因「模型点了不存在的工具名」而做的纠错重试次数（0 = 没发生）。
            # 它是"提示词/工具集是否让模型困惑"的直接信号，值得在 run_detail 里可见。
            "invalid_tool_retries": invalid_tool_retries,
        },
    }
