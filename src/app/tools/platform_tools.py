"""Agent Tools：把数据中台 RESTful 指标/动作能力封装成 LangChain 工具。

要点：
- 所有工具都通过 HTTP 调用数据中台（Agent 不直连中台库，符合数据服务层设计）；
- 工具按请求构造（build_platform_tools(user)），在工具内部做部门权限校验，避免跨部门数据泄露；
- 高危补货（qty≥阈值或金额≥阈值）由中台返回 pending_confirmation，等待人工确认。
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
import logging
import pathlib
import re
import time
import typing
import uuid

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from app.agents.query_plan import build_query_plan
from app.agents.tool_guard import with_tool_loop_guard
from app.auth import AuthUser
from app.cache.ttl_cache import get_ttl_cache
from app.config import get_settings
from app.db.engine import session_scope
from app.mcp_client.client import get_mcp_hub
from app.memory import org_memory, session_memory, workspace
from app.memory.runtime import current_tool_context, current_turn_log
from app.memory.tool_contract import with_result_contract
from app.observability import latency
from app.observability import trace as trace_mod
from app.resilience import Breaker
from app.security.scope import (
    SCOPE_KINDS,
    ensure_order_visible,
    ensure_sku_visible,
    ensure_style_visible,
    filter_visible_items,
    visible_categories,
)
from app.tools import catalog
from app.tools import platform_contract as contract

logger = logging.getLogger("sku-agent.tools")

# ---------------------------------------------------------------- 工具清单（来自能力目录）
# 唯一真相在**中台**（`data_platform/capabilities.py`），Agent 用提交到仓库的快照离线启动：
#   python scripts/gen_capabilities_snapshot.py     # 中台改了就重新生成
# `tests/test_capabilities_sync.py` 保证「快照 == 中台模块」，改漏一边即失败；
# `sync_capabilities()` 在启动时与中台 `/metadata/capabilities` 对一次版本，漂移写进 health。
CAPABILITY_SNAPSHOT = pathlib.Path(__file__).resolve().parent / "capabilities.snapshot.json"


def _load_capability_snapshot() -> dict:
    try:
        return json.loads(CAPABILITY_SNAPSHOT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover —— 快照被删才可能走到
        logger.error("能力目录快照读取失败（%s）：工具集将为空，请运行 "
                     "scripts/gen_capabilities_snapshot.py", exc)
        return {"version": "", "groups": {}, "reserved_groups": {}, "capabilities": []}


_SNAPSHOT: dict = _load_capability_snapshot()

# 分组 → 工具名。空分组会让 `tools: ["该组"]` 解析出空集（早先会静默退化成"全部工具"）
TOOL_GROUPS: dict[str, list[str]] = {
    group: sorted(names) for group, names in (_SNAPSHOT.get("groups") or {}).items()
}
# 预留分组：声明即报错（生图 Agent 的工具尚未实现，不能假装它可用）
RESERVED_GROUPS: dict[str, str] = dict(_SNAPSHOT.get("reserved_groups") or {})
# 工具名 → 部门作用域种类（唯一执行依据，见 scope_guard）
TOOL_SCOPES: dict[str, str] = {
    str(item["tool"]): str(item["scope_kind"]) for item in (_SNAPSHOT.get("capabilities") or [])
}
ALL_TOOL_NAMES: set[str] = {name for names in TOOL_GROUPS.values() for name in names}
_GROUP_OF: dict[str, str] = {
    name: group for group, names in TOOL_GROUPS.items() for name in names
}

# 与中台对版本的结果（漂移时写进 /api/health，而不是静默用过时快照）
_CAPABILITY_STATUS: dict = {
    "snapshot_version": _SNAPSHOT.get("version", ""),
    "remote_version": "",
    "checked": False,
    "drift": [],
    "error": "",
}


def capability_status() -> dict:
    return {**_CAPABILITY_STATUS, "tools": len(ALL_TOOL_NAMES), "groups": len(TOOL_GROUPS)}


def group_of(tool_name: str) -> str:
    return _GROUP_OF.get(tool_name, "")


async def sync_capabilities() -> dict:
    """启动时与中台对一次能力目录（失败不阻塞启动，只标注 degraded）。"""
    try:
        data = await get_platform_client().get("/api/v1/metadata/capabilities")
    except Exception as exc:  # noqa: BLE001 —— 中台未就绪/离线：继续用快照
        _CAPABILITY_STATUS.update({"checked": True, "error": f"{type(exc).__name__}: {exc}"})
        return capability_status()
    if not isinstance(data, dict) or data.get("error"):
        _CAPABILITY_STATUS.update({"checked": True,
                                   "error": str((data or {}).get("error") or "空响应")})
        return capability_status()
    remote = data.get("capabilities") or []
    remote_groups = data.get("groups") or {}
    drift: list[str] = []
    if remote_groups != TOOL_GROUPS:
        only_local = {k: v for k, v in TOOL_GROUPS.items() if remote_groups.get(k) != v}
        drift.append(f"分组不一致（本地快照缺/多）：{sorted(only_local)}")
    remote_tools = {str(item.get("tool")) for item in remote}
    if remote_tools - ALL_TOOL_NAMES:
        drift.append(f"中台有、本地快照没有的工具：{sorted(remote_tools - ALL_TOOL_NAMES)}")
    if ALL_TOOL_NAMES - remote_tools:
        drift.append(f"本地快照有、中台没有的工具：{sorted(ALL_TOOL_NAMES - remote_tools)}")
    _CAPABILITY_STATUS.update({
        "checked": True,
        "remote_version": str(data.get("version") or ""),
        "drift": drift,
        "error": "",
    })
    if drift:
        logger.warning("能力目录与本地快照不一致（请重新生成快照）：%s", drift)
    return capability_status()


def capability_catalogue() -> dict:
    """给注册表/健康检查用的能力目录快照。"""
    return {**_SNAPSHOT, "status": capability_status()}


# 后台任务集合：保持引用，避免 fire-and-forget 的 task 被 GC 掉
_BACKGROUND_TASKS: set = set()


def _spawn_background(coro) -> None:  # noqa: ANN001 —— 协程
    """把「不阻塞本轮答复」的收尾工作丢到后台（例如档案主文档的摘要整合）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:            # 没有事件循环（同步脚本里调用工具）→ 直接跳过
        return
    task = loop.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


# ---------------------------------------------------------------- 作用域声明（唯一执行依据）
# 为什么要有这张表：之前每个工具函数自己在函数体里写一行 `_scope_error(user, sku_id)`，
# 结果**只有带 sku_id 的工具**受保护——`list_pending_approvals`（无 sku_id）把别的部门的
# 待审批单返回给了部门账号；`get_sales_rate` 的 department 由模型随便填。
# 现在改成「声明 + 统一守卫」：装配时按这里的 kind 套校验，漏声明直接装配失败。
# 取值见 app.security.scope.SCOPE_KINDS：
#   sku        → 首个 sku_id 入参必须对当前账号可见
#   order      → order_id 反查 SKU 后再判可见性
#   sku_list   → 列表类返回体按 sku_id 过滤（跨部门条目剔除），并标注是否被过滤
#   department → department 入参只能取本部门（部门账号强制覆盖，admin 透传）
#   global     → 全局只读元数据，无业务数据作用域
#   local      → 本地文件/技能/计划，不涉及业务数据
#   org        → L3 组织记忆：函数体内按 departments 参数实现（此处仅登记便于审计）
#   style      → style_id 反查款式品类后再判可见性（款级指标/尺码结构）
# 注意：这张表**不再手写**——它由能力目录快照生成（见上方 TOOL_SCOPES），
# 这样"新增能力忘了声明作用域"在生成快照时就失败了，而不是等到某个部门账号越权。


def _assert_tool_scopes() -> None:
    """导入时自检：分组非空、作用域声明齐全且合法、预留分组没被当成分组用。

    放在导入期而不是请求期：漏声明是开发期错误，应该在 CI / 启动时就炸，
    而不是等生产上某个部门账号问出一份别的部门的数据。
    """
    if not TOOL_GROUPS:
        raise RuntimeError(
            "工具集为空：能力目录快照缺失或不合法，请运行 "
            "python scripts/gen_capabilities_snapshot.py"
        )
    empty_groups = sorted(group for group, names in TOOL_GROUPS.items() if not names)
    if empty_groups:
        raise RuntimeError(
            f"空工具分组 {empty_groups}：空分组会让 `tools:[\"该组\"]` 解析出空集，"
            "历史上正是它让 Agent 静默拿到了全部工具。要么补工具，要么改成 RESERVED_GROUPS"
        )
    leaked = sorted(set(TOOL_GROUPS) & set(RESERVED_GROUPS))
    if leaked:
        raise RuntimeError(f"预留分组 {leaked} 不能出现在可用分组里（它们尚未实现）")
    missing = sorted(ALL_TOOL_NAMES - set(TOOL_SCOPES))
    extra = sorted(set(TOOL_SCOPES) - ALL_TOOL_NAMES)
    if missing or extra:
        raise RuntimeError(
            f"TOOL_SCOPES 与工具清单不一致：缺声明={missing}，多余声明={extra}；"
            "新增能力必须在中台 capabilities.py 里声明 scope_kind"
        )
    bad = sorted({name: kind for name, kind in TOOL_SCOPES.items() if kind not in SCOPE_KINDS}.items())
    if bad:
        raise RuntimeError(f"TOOL_SCOPES 里有非法作用域 {bad}；可用值 {sorted(SCOPE_KINDS)}")


_assert_tool_scopes()


def _bound_arguments(fn, args: tuple, kwargs: dict) -> dict:
    """尽量把 (args, kwargs) 归一成参数字典（StructuredTool 走 kwargs，脚本可能走位置参数）。"""
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        return {k: v for k, v in bound.arguments.items() if k not in ("self", "cls")}
    except (TypeError, ValueError):
        return dict(kwargs)


def _apply_list_scope(user: AuthUser | None, data) -> dict:  # noqa: ANN001
    """列表类返回体按 sku_id 做部门过滤；标注是否应用了作用域、丢掉了多少条。"""
    if not isinstance(data, dict):
        return data
    items = data.get("items")
    if not isinstance(items, list):
        return data
    kept, dropped = filter_visible_items(user, items)
    out = dict(data)
    out["items"] = kept
    out["count"] = len(kept)
    out["scope_filtered"] = visible_categories(user) is not None
    out["scope_dropped"] = dropped
    return out


def _force_department(user: AuthUser | None, kwargs: dict) -> tuple[dict, bool]:
    """部门账号的 department 入参强制为本部门（模型填别的部门也不生效）；admin 透传。"""
    if user is None or user.is_admin or not user.department:
        return kwargs, False
    requested = str(kwargs.get("department") or "")
    forced = bool(requested and requested != user.department)
    args = dict(kwargs)
    args["department"] = user.department
    return args, forced


def scope_guard(tool_name: str, user: AuthUser | None):
    """按 TOOL_SCOPES 声明给工具套上统一的部门作用域校验。

    用法（见 build_platform_tools）：`scope_guard(name, user)(fn)`。
    - 用 functools.wraps 保留签名/注解：LangChain 构造 args_schema 依赖 `inspect.signature`；
    - 只处理「有作用域」的 kind，`global/local/org` 直通（org 的作用域仍在函数体内）。
    """
    kind = TOOL_SCOPES.get(tool_name)
    if kind is None:
        raise RuntimeError(
            f"工具 {tool_name} 未在 TOOL_SCOPES 声明作用域：无法保证部门隔离，装配直接失败"
        )

    def decorator(fn):
        if kind not in {"sku", "order", "style", "sku_list", "department"}:
            return fn

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            bound = _bound_arguments(fn, args, kwargs)
            if kind == "sku":
                err = ensure_sku_visible(user, bound.get("sku_id"))
                if err is not None:
                    return err
                return await fn(*args, **kwargs)
            if kind == "order":
                err = await ensure_order_visible(user, bound.get("order_id"))
                if err is not None:
                    return err
                return await fn(*args, **kwargs)
            if kind == "style":
                err = ensure_style_visible(user, bound.get("style_id"))
                if err is not None:
                    return err
                return await fn(*args, **kwargs)
            if kind == "kb":
                # 知识库按「本部门 + 通用」过滤（作用域在函数体内按 user 生效）
                return await fn(*args, **kwargs)
            if kind == "department":
                scoped, forced = _force_department(user, bound)
                data = await fn(**scoped)
                if isinstance(data, dict) and forced:
                    data = {**data, "department_forced": True,
                            "note": f"department 已按当前账号权限限定为 {scoped.get('department')}"}
                return data
            return _apply_list_scope(user, await fn(*args, **kwargs))

        return wrapper

    return decorator


def tool_catalogue() -> dict:
    """给注册表/健康检查用的工具目录快照。"""
    return {
        "groups": {group: list(names) for group, names in TOOL_GROUPS.items()},
        "tools": sorted(ALL_TOOL_NAMES),
        "count": len(ALL_TOOL_NAMES),
        # 每个工具的部门作用域种类：审计「哪些工具会碰业务数据、怎么校验」
        "scopes": dict(sorted(TOOL_SCOPES.items())),
        "reserved_groups": dict(RESERVED_GROUPS),
        "capabilities": capability_status(),
    }


def resolve_tool_selection(selection: list[str] | set[str] | None) -> set[str] | None:
    """把 manifest 的 tools 声明解析成工具名集合。

    语义（评审 1.4 修复，三处缺一不可）：
    - `None`（字段缺失）或 `["*"]` → **None = 全量**，这是唯一的"全量"入口；
    - 显式声明（含空列表）解析出的集合**为空则直接抛错**——早先空集合会被下游当成
      "没声明"从而回退全量，于是 `tools: ["image"]`（image 分组是空的）的 Agent
      静默拿到了全部 22 个工具，而 run_detail 里还写着 tool_count=0；
    - 预留分组（image）声明即报错：功能没实现就不该"看起来能用"；
    - 未知名字直接抛错：拼错工具名要在加载期失败，而不是静默少一个工具。
    """
    if selection is None:
        return None
    items = sorted(selection) if isinstance(selection, set) else list(selection)
    if not items:
        raise ValueError(
            'tools 显式为 []：它会解析成「零工具」而不是「全量」；'
            '要全量请写 ["*"]，要只读请写 ["read"]'
        )
    if "*" in items:
        return None
    chosen: set[str] = set()
    unknown: list[str] = []
    reserved: list[str] = []
    for item in items:
        if item in RESERVED_GROUPS:
            reserved.append(f"{item}（{RESERVED_GROUPS[item]}）")
        elif item in TOOL_GROUPS:
            chosen.update(TOOL_GROUPS[item])
        elif item in ALL_TOOL_NAMES:
            chosen.add(item)
        else:
            unknown.append(item)
    if reserved:
        raise ValueError(f"声明了预留但未实现的分组：{reserved}")
    if unknown:
        raise ValueError(f"未知工具/分组：{unknown}；可用分组 {sorted(TOOL_GROUPS)}")
    if not chosen:
        raise ValueError(
            f"tools 声明 {items} 解析后一个工具都没有：拒绝装配（空工具集不等于全量工具）"
        )
    return chosen


def expected_tool_count(selection: list[str] | set[str] | None) -> int:
    """按与装配**同一条解析路径**算工具数（供 run_detail 报数，不允许另算一套）。

    早先这里用的是 `len(manifest.tool_names)`，而装配走的是 `resolve_tool_selection(allow)`；
    两条路径在「空集」上语义不同，于是出现「日志说 0 个工具、实际装了 22 个」。
    现在两者都从 `resolve_tool_selection` 出发，`tests/test_tool_selection.py` 还会断言
    `len(build_platform_tools(...)) == expected_tool_count(...)`。
    """
    selected = resolve_tool_selection(selection)
    return len(ALL_TOOL_NAMES) if selected is None else len(selected)


def _cache_key(path: str, params: dict | None, *, origin: str = "") -> str:
    """Agent 旁路缓存 key：接口 + 参数 + **中台来源**。

    为什么要带 `origin`：只按 path 做 key 时，切换中台地址（蓝绿发布 / 本地改连另一个中台）
    会命中上一个中台的缓存——包括负缓存里的"连不上"，表现为"换了地址还是报旧错"。
    """
    digest = hashlib.sha1(
        json.dumps(params or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    origin_tag = hashlib.sha1((origin or "").encode("utf-8")).hexdigest()[:8] if origin else "local"
    return f"agent:platform:{origin_tag}:{path}:{digest}"


def _new_request_id() -> str:
    """中台调用关联 id = `{trace_id}:{随机后缀}`。

    trace_id 就是本轮的 turn_id（见 app/observability/trace.py），所以一次用户提问
    产生的所有中台审计行都能按前缀捞出来——这是「一轮对话 ↔ 一串外部调用」的对账锚点。
    """
    turn = trace_mod.current_trace_id()
    suffix = uuid.uuid4().hex[:6]
    return f"{turn}:{suffix}" if turn else suffix


def _error_detail(resp: httpx.Response) -> str:
    try:
        detail = resp.json().get("detail", "")
    except Exception:  # noqa: BLE001
        detail = resp.text[:200]
    return str(detail)[:300]


# 有副作用的工具（会建草稿单 / 工单）。默认只有 admin 能调用，见 config.write_tools_roles。
WRITE_TOOL_NAMES = {"create_replenishment_order", "create_after_sales", "save_analysis"}


def _make_writer_denied(name: str, original):
    """把写工具替换成「明确拒绝」的桩：保留函数签名（LangChain 要拿 schema），行为固定返回拒绝。

    为什么不在系统提示里「要求模型不要写」：提示词是建议，工具白名单才是机制。
    没有这个函数，部门用户只要模型愿意，就能让 Agent 建补货单/售后单。
    """
    import inspect

    signature = inspect.signature(original)

    async def _denied(*args, **kwargs) -> dict:
        return {
            "error": (
                f"无权限执行写操作：{name} 需要写权限账号（默认仅 admin）。"
                "当前账号可以查看数据、发起申请，但不能直接建单。"
            ),
            "error_kind": "business",
            "denied_tool": name,
        }

    _denied.__signature__ = signature  # type: ignore[attr-defined]
    # pydantic/LangChain 取参数类型时读的是被包装函数的 __annotations__，
    # 必须一起复制，否则构造 args_schema 会 KeyError（参数名有、类型注解没有）。
    _denied.__annotations__ = dict(getattr(original, "__annotations__", {}) or {})
    _denied.__name__ = getattr(original, "__name__", name)
    return _denied


class PlatformClient:
    """中台 REST 客户端。

    性能要点：`httpx.AsyncClient` **必须复用**——每调用新建一次在本机实测要 0.5~1.4s
    （httpx 初始化连接池、且 `trust_env=True` 会去读 Windows 注册表代理配置），
    而真正的请求只要 5~8ms。这里用长生命周期 client + `trust_env=False`（代理需显式配置）。
    """

    def __init__(self, base_url: str | None = None, token: str | None = None, transport=None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.platform_api_url).rstrip("/")
        self.token = token or settings.platform_internal_token
        # 调用方身份：中台按 agent_id 做授权/配额/审计（一个中台可以接多个 Agent）
        self.agent_id = (getattr(settings, "platform_agent_id", "") or "sku-agent")[:32]
        self._transport = transport
        self._timeout = float(settings.platform_timeout_seconds)
        self._proxy = settings.platform_proxy or None
        self._client: httpx.AsyncClient | None = None
        # 记录 client 是绑在哪个事件循环上创建的：脚本里连续多次 asyncio.run 时，
        # 复用上一个（已关闭的）循环的连接池会报 "Event loop is closed"。
        # 生产是单长驻循环，这里只是让脚本/测试的用法不至于静默失败。
        self._client_loop: object | None = None
        # 熔断：与 MCP 同一套实现（app/resilience.Breaker，半开只放一个探测）。
        # 中台是整个 Agent 最主要的依赖（10 个只读工具都走它），
        # 没有熔断时"中台挂了"的表现是每个工具都先等满 15s 超时。
        self._breaker = Breaker(
            threshold=int(getattr(settings, "platform_breaker_threshold", 3)),
            reset_seconds=float(getattr(settings, "platform_breaker_reset_seconds", 60.0)),
        )

    def breaker_state(self) -> dict:
        return self._breaker.state()

    def _get_client(self) -> httpx.AsyncClient:
        """懒加载并复用同一个 AsyncClient（连接池复用，零握手开销）。"""
        try:
            loop: object | None = asyncio.get_running_loop()
        except RuntimeError:            # 没有运行中的循环（同步调用）
            loop = None
        stale_loop = (
            loop is not None and self._client_loop is not None and self._client_loop is not loop
        )
        if self._client is None or self._client.is_closed or stale_loop:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                proxy=self._proxy,          # 空=不走代理
                trust_env=False,            # 不读系统代理（否则 127.0.0.1 也会被绕进代理）
            )
            self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        self._client_loop = None

    async def request(self, method: str, path: str, **kwargs) -> dict:
        # 只读 GET 走 Agent 旁路缓存（TTL 随机抖动），写操作一律直连中台
        if method.upper() == "GET" and not kwargs.get("no_cache"):
            return await self._cached_get(path, kwargs.get("params"))
        return await self._do_request(method, path, **kwargs)

    async def _cached_get(self, path: str, params: dict | None) -> dict:
        settings = get_settings()
        cache = get_ttl_cache()
        key = _cache_key(path, params, origin=f"{self.base_url}|{self.agent_id}")
        cached = await cache.aget(key)     # to_thread：同步 Redis 读不占事件循环
        if cached is not None:
            payload = dict(cached)
            negative = "error" in payload
            payload["agent_cached"] = not negative
            payload["negative_cached"] = negative
            return payload
        data = await self._do_request("GET", path, params=params)
        if "error" not in data and data:
            await cache.aset(key, data, settings.cache_ttl_platform)
        elif data.get("link_error"):
            # 负缓存（短 TTL）：中台整体不可用时不至于每轮都先等满超时再降级
            await cache.aset(key, data, max(1, int(getattr(settings, "cache_ttl_negative", 10))))
        payload = dict(data)
        payload["agent_cached"] = False
        payload["negative_cached"] = False
        return payload

    async def _do_request(self, method: str, path: str, **kwargs) -> dict:
        # 关联 id：中台审计行里能查到同一条 request_id，Agent 轮次 ↔ 中台调用可对账
        request_id = _new_request_id()
        headers = {
            "X-Internal-Token": self.token,
            "X-Api-Key": self.token,
            "X-Agent-Id": self.agent_id,
            # 主动声明自己的契约版本：中台发现主版本不一致会在响应头给出 warning
            "X-Agent-Contract": contract.AGENT_CONTRACT_VERSION,
            "X-Request-Id": request_id,
            # 同一 trace 下的所有调用共享前缀，便于在中台审计里一次捞全
            "X-Trace-Id": trace_mod.current_trace_id(),
        }
        started = time.perf_counter()
        if not self._breaker.allow():
            state = self._breaker.state()
            latency.record("platform", (time.perf_counter() - started) * 1000)
            return {
                "error": (f"数据中台熔断中（{state['retry_in_s']}s 后重试）："
                          f"最近连续 {state['failures']} 次链路失败"),
                "error_kind": "transport",
                "breaker_open": True,
                "request_id": request_id,
                # 给模型看的「下一步」：否则它会在同一轮里反复调同一个工具撞熔断，白烧轮次
                "next_step": ("不要再调用任何取数工具：直接告诉用户「数据中台暂时不可用，"
                              "请在 1 分钟后重试」，如需继续可先给出口径说明"),
            }
        try:
            resp = await self._get_client().request(
                method, self.base_url + path, headers=headers, **kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            latency.record("platform", (time.perf_counter() - started) * 1000)
            self._breaker.record_failure()
            return {"error": f"数据中台不可用：{type(exc).__name__}: {exc}",
                    "error_kind": "transport", "link_error": True, "request_id": request_id,
                    "next_step": "这是链路故障（不是数据问题）：不要原地重试同一工具，"
                                 "直接告知用户稍后再试"}
        finally:
            self._breaker.release_probe()
        latency.record("platform", (time.perf_counter() - started) * 1000)
        # 中台会把 request_id 原样回显；优先用它（同一份审计证据）
        request_id = resp.headers.get("X-Request-Id") or request_id
        if resp.status_code >= 500:
            # 5xx = 中台的链路健康问题，计入熔断（但不做负缓存：5xx 返回很快，
            # 缓存它反而会掩盖中台恢复）；4xx 是调用方的错，不该拖垮熔断
            self._breaker.record_failure()
            detail = _error_detail(resp)
            return {"error": f"数据中台接口错误 HTTP {resp.status_code}: {detail}",
                    "error_kind": "transport", "server_error": True,
                    "status_code": resp.status_code, "request_id": request_id}
        if resp.status_code == 401:
            # 令牌/身份问题：不可重试，也不该计入熔断（中台是好的，是我们的 key 不对）
            return {"error": f"中台拒绝了本次调用（401）：{_error_detail(resp)}",
                    "error_kind": "auth", "status_code": 401, "request_id": request_id,
                    "next_step": "这是配置问题（中台令牌），不要重试；提示用户联系管理员"}
        if resp.status_code == 403:
            return {"error": f"中台拒绝了本次调用（403，越权或缺 scope）：{_error_detail(resp)}",
                    "error_kind": "permission", "status_code": 403, "request_id": request_id,
                    "next_step": "这是权限问题：直接告知用户无权限，不要换个参数重试同一工具"}
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "")
            return {"error": f"中台限流（429）：{_error_detail(resp)}",
                    "error_kind": "ratelimited", "status_code": 429, "request_id": request_id,
                    "retry_after_s": retry_after,
                    "next_step": "调用配额已用尽：告知用户稍后再试，不要在同一轮里连续重试"}
        if resp.status_code >= 400:
            return {"error": f"数据中台接口错误 HTTP {resp.status_code}: {_error_detail(resp)}",
                    "error_kind": "transport", "status_code": resp.status_code, "request_id": request_id}
        payload = resp.json()
        # 契约校验放在熔断记账之前：版本不匹配既不是链路故障也不是数据问题，
        # 不该把中台判成"不健康"（否则会连带熔断掉本来正常的接口）。
        contract_error = contract.check(
            payload, resp.headers.get("X-Platform-Contract", ""),
            strict=bool(getattr(get_settings(), "platform_contract_strict", False)),
        )
        if contract_error is not None:
            return {**contract_error, "request_id": request_id,
                    "remote_contract": contract.stats().get("last_remote", "")}
        self._breaker.record_success()
        if isinstance(payload, dict):
            payload["request_id"] = request_id
            payload.setdefault("schema_version", contract.AGENT_CONTRACT_VERSION)
        return payload

    async def get(self, path: str, params: dict | None = None) -> dict:
        return await self.request("GET", path, params=params)

    async def get_fresh(self, path: str, params: dict | None = None) -> dict:
        """绕过 Agent 缓存直取（调试/需要最新时点用）。"""
        return await self._do_request("GET", path, params=params)

    async def post(self, path: str, payload: dict) -> dict:
        return await self.request("POST", path, json=payload)


_client: PlatformClient | None = None


def get_platform_client() -> PlatformClient:
    global _client
    if _client is None:
        _client = PlatformClient()
    return _client


def set_platform_client(client: PlatformClient | None) -> None:
    global _client
    _client = client


async def aclose_platform_client() -> None:
    """关闭复用的 httpx 连接池（应用 shutdown 时调用）。"""
    if _client is not None:
        await _client.aclose()


def _scope_departments(user: AuthUser | None) -> list[str] | None:
    """L3 组织记忆的部门作用域：管理员 None（全量），部门用户取其品类集合。"""
    return visible_categories(user)


# ---------------------------------------------------------------- 档案部门推断（复审 F1）
# 组织记忆按**部门**隔离（`本部门 + 「全员」`），而「全员」对所有部门账号可见。
# 早先 `save_analysis` 用"是谁在问"决定部门（admin 的 user.department 是 None → 落「全员」），
# 于是 admin 做的**单部门**分析（含 SKU 级数字）会被全部门读到 —— 这与项目在追问线程上
# 已经确立的原则冲突（"校验了参数，也要校验数据来源"）。
# 现在改为**按内容推断部门**：本轮取过的 SKU / 款号 / 部门级入参，谁的就归谁；
# 只有真正的跨部门结论才落「全员」。
_ARCHIVE_SKU_RE = re.compile(r"\bST-\d{2}-C\d{1,2}-(?:[SMLX]{1,3}|\d{2,3})\b", re.I)
_ARCHIVE_STYLE_RE = re.compile(r"\bST-\d{2}\b", re.I)
_ARCHIVE_DEPT_ARG_RE = re.compile(r'"department"\s*:\s*"([^"]{1,12})"')


def _archive_evidence_text(question: str, conclusion: str, evidence: list[str]) -> str:
    """本轮"内容证据"文本：取数步骤的入参 + 问题 + 结论 + 证据。

    注意 `steps[].args` 存的是 **JSON 字符串**（`tool_contract` 里用 `_short_json` 序列化过），
    所以这里统一按文本拼，再用正则抽 SKU / 款号 / 部门入参。
    """
    steps = (current_turn_log() or {}).get("steps") or []
    parts = [str((s or {}).get("args") or "") for s in steps if isinstance(s, dict)]
    parts.extend([question or "", conclusion or ""])
    parts.extend(str(e) for e in (evidence or []))
    return "\n".join(parts)


def _writable_departments(user: AuthUser | None) -> set[str] | None:
    """账号可写档案的部门集合（None = 不限，即管理员/匿名）。

    部门账号的可见品类（`visible_categories`，女装含童装）要**反查回部门名**——
    档案是按部门存的，不能直接拿品类名当部门（平台映射不同名时会错）。
    """
    if user is None or user.is_admin:
        return None
    from app.config import department_of

    return {dept for dept in (department_of(c) for c in (visible_categories(user) or []))
            if dept}


def _infer_archive_department(user: AuthUser | None, *, question: str, conclusion: str,
                              evidence: list[str]) -> tuple[str | None, str, str]:
    """推断这份档案该归哪个部门：返回 (department | None, source, note)。

    - `source="inferred_sku" / "inferred_style" / "inferred_arg"`：内容里有唯一部门证据；
    - `source="user"`：内容无部门证据（或推断越界），退回账号自己所属部门；
    - `source="all"`：跨部门结论（admin 且无部门证据）→ 「全员」。

    **非管理员收紧（复审 S2）**：推断结果必须落在账号可写部门内。早先只看内容——
    男装账号问一句女装 SKU 就会把档案写成「女装」（现在没有写权限所以打不通，但一旦
    `WRITE_TOOLS_ROLES` 开放写权限、或脚本/手工路径复用这个 helper 就会越界）。
    """
    from app.config import all_departments, department_of

    text = _archive_evidence_text(question, conclusion, evidence)
    known = set(all_departments())
    found: list[tuple[str, str]] = []   # (department, source)

    for sku_id in {m.upper() for m in _ARCHIVE_SKU_RE.findall(text)}:
        dept = department_of(catalog.sku_category(sku_id))
        if dept in known:
            found.append((dept, "inferred_sku"))
    for style_id in {m.upper() for m in _ARCHIVE_STYLE_RE.findall(text)}:
        dept = department_of(catalog.style_category(style_id))
        if dept in known:
            found.append((dept, "inferred_style"))
    for token in _ARCHIVE_DEPT_ARG_RE.findall(text):
        token = token.strip()
        if token in known:
            found.append((token, "inferred_arg"))

    departments = sorted({dept for dept, _ in found})
    writable = _writable_departments(user)
    own = "" if writable is None else (user.department or "")
    if writable is not None:
        outside = [d for d in departments if d not in writable]
        if outside:
            target = own or next(iter(sorted(writable)), "")
            return target, "user", (
                f"内容指向「{'、'.join(outside)}」部门，但当前账号只能写本部门"
                f"（{target or '未知'}），已按本部门沉淀；跨部门结论请用管理员账号"
            )

    if len(departments) == 1:
        dept = departments[0]
        source = next(src for d, src in found if d == dept)
        return dept, source, ""
    if len(departments) > 1:
        return None, "all", f"跨部门结论（{'、'.join(departments)}），按「全员」沉淀"
    # 内容里**有** SKU/款号但一个都没解析出部门：目录可能没同步/本地库没有该 SKU。
    # 宁可在返回里把这件事说清楚（模型可以转告用户），也不假装"没有部门证据"。
    has_tokens = bool(_ARCHIVE_SKU_RE.search(text) or _ARCHIVE_STYLE_RE.search(text))
    if has_tokens and not own:
        return None, "all", "内容里有 SKU/款号但目录查不到所属部门（可能目录未同步），按「全员」沉淀"
    if own:
        return own, "user", "内容里没有可判定的部门证据，按账号所属部门沉淀"
    return None, "all", "内容里没有部门证据，按「全员」沉淀（全部门可见）"



# ---------------------------------------------------------------- 模型可见的工具参数说明（复审 A1）
# 为什么需要：`StructuredTool.from_function(description=...)` 会**丢掉函数 docstring**，
# 而按签名推断出的 args_schema 只有字段名与类型 —— 模型看不到 `topk / days / qty / reason`
# 是什么意思，只能靠猜；`save_analysis` 的"仅在用户明确要求沉淀时调用"更是完全不可见。
# 这里给每个**入参**补一句说明（与签名同源生成 schema，不维护第二份定义），
# `scripts/eval_agent_tooling.py` 会断言"每个参数都有说明"，防止以后再漏。
PARAM_DOCS: dict[str, str] = {
    "sku_id": "SKU 编号，形如 ST-01-C1-M（款-颜色-尺码）",
    "style_id": "款号，形如 ST-01（同款所有颜色/尺码共用一个款号）",
    "order_id": "订单号，形如 SO07208742",
    "analysis_id": "分析档案 id（由 search_analyses 返回）",
    "department": "部门名（男装/女装）；部门账号会被强制为本部门，填别的也不生效",
    "days": "回溯天数（近 N 天）",
    "limit": "返回条数上限（越大越容易被聚合截断，优先取小值再看 summary）",
    "topk": "检索返回片段数，建议 3~5，上限 8",
    "qty": "数量（件），整数；动作类工具必填",
    "reason": "动作原因/依据，会写进单据，用业务语言简述",
    "name": "Skill 名称，如 size-gap-diagnosis；未知名称会返回可用清单",
    "query": "检索问句（保留用户原话与实体，不要改写成结论）",
    "question": "原始用户问题（档案可追溯用）",
    "conclusion": "结论正文（一句话结论 + 关键数字，含 result_id）",
    "key_metrics": '关键指标字典，如 {"available": 42, "sell_through_days": 12}',
    "evidence": "证据列表：数据结论与规则引用（含 result_id / chunk_id）",
    "title": "档案标题（可留空，系统按问题生成）",
}

# 个别工具对同名参数有额外语义（覆盖 PARAM_DOCS）
PARAM_DOCS_OVERRIDE: dict[str, dict[str, str]] = {
    "get_size_gap": {"department": "部门名；部门账号会被强制为本部门"},
    "get_apparel_overview": {"department": "部门名；部门账号会被强制为本部门"},
    "get_sales_rate": {
        "department": "部门名；部门账号会被强制为本部门（返回 department_forced=true）",
    },
    "create_replenishment_order": {
        "qty": "补货数量（件）；≥ 人工确认阈值会返回 pending_confirmation",
        "reason": "补货原因，会写进补货草稿单",
    },
}


def _tool_args_schema(fn, tool_name: str) -> type[BaseModel] | None:  # noqa: ANN001 —— 内部工具函数
    """按工具签名生成带**参数说明**的 args_schema（复审 A1）。

    与 `infer_schema` 用的是同一份签名与注解，只是额外挂上 `Field(description=...)`；
    注解无法求值/生成失败时返回 `None`，调用方回退签名推断，不阻塞装配
    （缺说明会被 `scripts/eval_agent_tooling.py` 的门槛暴露出来，而不是静默降质）。
    """
    try:
        hints = typing.get_type_hints(fn)
        overrides = PARAM_DOCS_OVERRIDE.get(tool_name) or {}
        fields: dict[str, tuple] = {}
        for name, param in inspect.signature(fn).parameters.items():
            if name in ("self", "cls"):
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            annotation = hints.get(name, str)
            default = ... if param.default is inspect.Parameter.empty else param.default
            doc = overrides.get(name) or PARAM_DOCS.get(name, "")
            fields[name] = (annotation, Field(default, description=doc) if doc else default)
        if not fields:
            return None
        return create_model(f"{tool_name}_args", **fields)
    except Exception as exc:  # noqa: BLE001 —— 生成失败回退签名推断，不影响装配
        logger.debug("工具 %s 的参数说明生成失败（回退签名推断）：%s", tool_name, exc)
        return None


def build_platform_tools(user: AuthUser | None,
                        allow: list[str] | set[str] | None = None) -> list[StructuredTool]:
    client = get_platform_client()

    # 注意：部门作用域不再写在各工具函数体里（那样只有带 sku_id 的工具受保护）。
    # 统一由 build_platform_tools 按 TOOL_SCOPES 声明套 scope_guard，见文件末尾装配处。

    async def get_inventory(sku_id: str) -> dict:
        """查询 SKU 可用库存（中台治理口径：可用=现存-锁定-CRM预订未发货-订单未发货占用）。"""
        return await client.get(f"/api/v1/skus/{sku_id}/inventory")

    async def get_sell_through(sku_id: str) -> dict:
        """查询 SKU 预计售罄天数与缺货风险等级（可用库存+7天内在途 ÷ 日均销量）。"""
        return await client.get(f"/api/v1/skus/{sku_id}/sell-through")

    async def get_sku_metrics(sku_id: str) -> dict:
        """查询 SKU 治理指标：安全库存线、再订货点、动销标记、预计售罄等。"""
        return await client.get(f"/api/v1/skus/{sku_id}/metrics")

    async def get_in_transit(sku_id: str) -> dict:
        """查询 SKU 在途采购（数量/预计到货日/7天内到货量）。"""
        return await client.get(f"/api/v1/skus/{sku_id}/in-transit")

    async def get_reservations(sku_id: str) -> dict:
        """查询 SKU 的 CRM 客户预订（已脱敏客户信息），用于缺货影响评估。"""
        return await client.get(f"/api/v1/skus/{sku_id}/reservations")

    async def get_orders(sku_id: str, days: int = 7) -> dict:
        """查询 SKU 近 N 天订单（销量、待发货占用、脱敏收货人）。"""
        return await client.get(f"/api/v1/skus/{sku_id}/orders", params={"days": days})

    async def get_sales(sku_id: str, days: int = 7) -> dict:
        """查询 SKU 销量（近 7/30 天销量与日均销量）。"""
        return await client.get(f"/api/v1/skus/{sku_id}/sales", params={"days": days})

    async def get_metric_definitions() -> dict:
        """获取数据中台治理口径元数据（动销率/安全库存/预计售罄等公式与同义词）。"""
        return await client.get("/api/v1/metadata/metrics")

    async def get_sales_rate(department: str = "") -> dict:
        """查询动销率（近30天有销量SKU数 ÷ SKU总数），可按部门过滤。

        部门账号的 department 会被强制限定为本部门（返回值里 department_forced=true），
        避免模型填一个别的部门就把跨部门指标取出来。
        """
        params = {"department": department} if department else None
        return await client.get("/api/v1/metrics/sales-rate", params=params)

    async def create_replenishment_order(sku_id: str, qty: int, reason: str) -> dict:
        """创建补货草稿单（调用 ERP Mock 审批流）。大额补货（≥200件或≥5万元）会进入人工确认。

        幂等：同「会话/轮次 + SKU + 数量 + 原因」重复提交只建一单（键由系统计算，
        调用方不要自己编幂等键——模型自造的键会让同一请求重建成多张草稿单）。
        """
        ctx = current_tool_context()
        turn = (current_turn_log() or {}).get("turn_id", "")
        key_source = f"{ctx.get('session_id', '')}|{turn}|{sku_id}|{int(qty)}|{reason}"
        if not turn and not ctx.get("session_id"):
            key_source = f"{key_source}|{uuid.uuid4().hex}"  # 无会话上下文（脚本直调）时不去重
        idem_key = hashlib.sha1(key_source.encode("utf-8")).hexdigest()
        return await client.post("/api/v1/erp/replenish-orders", {
            "sku_id": sku_id, "qty": qty, "reason": reason,
            "requested_by": user.username if user else "agent",
            "idempotency_key": idem_key,
        })

    async def create_transfer_suggestion(sku_id: str) -> dict:
        """查询同款跨仓调拨建议（从其他仓库调货到主仓的可行数量）。"""
        return await client.post("/api/v1/transfer-suggestions", {"sku_id": sku_id})

    async def list_pending_approvals() -> dict:
        """查询当前待人工确认的补货草稿单（只返回本部门 SKU 的单据）。"""
        return await client.get("/api/v1/approvals", params={"status": "pending"})

    # ------------------------------------------------------------ 服装口径：款级 / 尺码结构
    # 为什么需要这几个工具：SKU 级"库存充足"经常是**断码压货**——主销码卖完了、其余码压着。
    # 只按 SKU 或按款平均都会得出错误结论，必须按「款 × 码」看。
    async def get_style_metrics(style_id: str) -> dict:
        """查询款级指标：生命周期阶段、尺码曲线、齐码/断码、周转天数、售罄率、款×色×码明细。"""
        return await client.get(f"/api/v1/styles/{style_id}/metrics")

    async def get_size_curve(style_id: str) -> dict:
        """查询某款的尺码曲线（各码销量占比；≥15% 为主销码）。"""
        return await client.get("/api/v1/metrics/size-curve", params={"style_id": style_id})

    async def get_size_gap(department: str = "", limit: int = 20) -> dict:
        """查询断码款清单（主销码缺货的款，含缺哪几个码）与部门齐码率/断码率。"""
        params: dict = {"limit": max(1, min(int(limit or 20), 100))}
        if department:
            params["department"] = department
        return await client.get("/api/v1/metrics/size-gap", params=params)

    async def get_apparel_overview(department: str = "") -> dict:
        """服装经营概览：齐码率、断码率、平均周转天数、平均售罄率、生命周期分布。"""
        params = {"department": department} if department else None
        return await client.get("/api/v1/metrics/apparel-overview", params=params)


    # ------------------------------------------------------------ L2 工作区记忆 / query_plan
    async def list_memory_skills() -> dict:
        """列出可用 Skills（SOP 式操作流程）；需要时再用 load_skill 加载全文。"""
        items = workspace.list_skills()
        return {"count": len(items), "items": items}

    async def load_skill(name: str) -> dict:
        """按需加载一个 skill 的完整操作流程（sku-screening / size-gap-diagnosis 等）。"""
        return workspace.load_skill(name)

    async def plan_query(question: str) -> dict:
        """把问题解析成结构化查询计划：意图/实体/时间窗/必需数据/置信度/plan_id。

        模糊指代（这个款、它）会先用本会话工作集回填，并标注 resolved_from 与 assumptions。
        """
        ctx = current_tool_context()
        working_set: dict = {}
        if ctx.get("session_id"):
            with session_scope() as session:
                snap = session_memory.get_memory_snapshot(session, ctx["session_id"])
                working_set = (snap or {}).get("working_set") or {}
        return build_query_plan(question, user=user, working_set=working_set)

    async def search_knowledge(query: str, topk: int = 5) -> dict:
        """检索知识库（SOP/规则/口径/历史案例），返回片段与引用 chunk_id。

        什么时候用：用户问「流程怎么走」「规则是什么」「有没有相关 SOP/案例」「上次类似情况怎么处理」。
        不要用它取业务数字（库存/销量走取数工具）。检索不到相关内容时会返回 no_answer=true，
        此时照实说「知识库没有相关内容」，不要拿通用常识冒充公司制度。
        命中里带 authority（治理口径/部门细则/通用规则/历史案例）；同一口径多份规定冲突时，
        conflicts 给出胜出的那份与理由——按它作答，不要把两个口径都念一遍。
        conflict_check 三态别混用：`unavailable`=没做比对（不得暗示已核对）、
        `none`=本次比对范围内无分歧（不代表制度无冲突；`conflict_diagnostics.single_source_keys`
        非空说明该口径只有单一来源）、`ok`=有冲突按 conflict_note 取一条。
        `department_overrides` 只能写成「若问的是 X 部门则为 Y」；
        `scope_notice` 非空时必须先告知用户可见范围（看不到某部门 / 多部门比较分别给出）。
        """
        from app.rag.facets import query_departments
        from app.rag.pipeline import search_kb
        from app.security.scope import kb_department_filter, kb_scope_label

        # 可见范围与 /api/rag/search **完全一致**（admin/匿名=全部，部门=本部门+通用）：
        # 早先这里自己算 dept，admin 被降级成「只看通用」→ 同问题两条路径答案不同，
        # 而且冲突检查会返回 none（看起来"已核对"）→ 自信地答错口径（复审 P1-1）。
        # 部门面（问题里出现男装/女装/童装）→ 管理员侧把检索收敛到「该部门 + 通用」：
        # 否则管理员问男装会召回女装细则，裁决出"45 天"（复审 P1）。
        departments = query_departments(query)
        filters = kb_department_filter(user, departments)
        # 范围自述也走同一个函数，避免"女装通用部门"这种拼接错误（复审 P2-2）
        scope_used = kb_scope_label(user, departments)
        result = await search_kb(query, filters=filters, topk=max(1, min(int(topk or 5), 8)))
        primary = result.is_primary
        return {
            "query": query,
            "count": len(result.hits),
            "no_answer": result.no_answer,
            # 检索范围自述：admin/匿名是"全部 SOP"（跨部门文档可能一起进 top-k，属设计而非越权）
            "scope_used": scope_used,
            "rerank": result.rerank,
            # 冲突是"结论的一部分"：不告诉模型它就会折中（评审 2.4）
            "conflicts": result.conflicts,
            "conflict_check": result.conflict_check,
            "conflict_note": result.conflict_note,
            # 自述：比对了哪些口径 / 哪些只有单一来源 / 池内无关条数。
            # `none` 只代表"比对过、没分歧"，`unavailable` 代表"没做比对"——别混用。
            "conflict_diagnostics": result.conflict_diagnostics,
            # 部门例外：问题没有部门面时，别的部门的取值只能作"若为 X 部门则 Y"的补充
            "department_overrides": result.department_overrides,
            # 检索范围说明：问的部门不在账号可见范围 / 多部门比较 / 跨部门比较意图时，必须先告知用户
            "scope_notice": result.scope_notice,
            # 未单独规定口径的部门（按通用口径执行）
            "uncovered_departments": result.uncovered_departments,
            "hits": [
                {"chunk_id": h.chunk_id, "doc_id": h.doc_id,
                 "title": h.meta.get("title", ""), "section": h.meta.get("section", ""),
                 "authority": h.meta.get("authority", ""),
                 "version": h.meta.get("version", ""),
                 "is_primary": bool(primary.get(h.chunk_id, True)),
                 "text": (h.meta.get("body") or h.text)[:600],
                 "score": round(h.score, 6), "rerank_score": round(h.rerank_score, 4)}
                for h in result.hits
            ],
            "note": ("知识库没有相关内容：请如实告知用户，并建议改用取数或询问业务方"
                     if result.no_answer else "引用时请标注 chunk_id；有冲突时按 conflict_note 取一条"),
        }

    # ------------------------------------------------------------ 订单 / 物流 / 售后（MCP）
    # 订单类工具的部门校验按 TOOL_SCOPES 声明为 "order"：order_id → SKU → 部门作用域，
    # 由 scope_guard 统一完成（原先每个函数都要 `await _order_scope_error(...)`）。

    async def get_order_status(order_id: str) -> dict:
        """查询订单状态（含物流与售后工单摘要）。"""
        return await get_mcp_hub().call("order", "get_order_status", {"order_id": order_id})

    async def list_orders(sku_id: str, days: int = 30) -> dict:
        """查询某 SKU 近 N 天的订单（按时间倒序）。"""
        return await get_mcp_hub().call("order", "list_orders", {"sku_id": sku_id, "days": days})

    async def get_logistics(order_id: str) -> dict:
        """查询订单物流状态与预计到货。"""
        return await get_mcp_hub().call("order", "get_logistics", {"order_id": order_id})

    async def create_after_sales(order_id: str, reason: str, qty: int | None = None) -> dict:
        """发起售后工单（幂等；返回 pending_confirmation，等待人工确认，不会自动退款）。"""
        args: dict = {"order_id": order_id, "reason": reason}
        if qty:
            args["qty"] = int(qty)
        # 写操作不走缓存：幂等由服务端 idempotency_key 保证
        return await get_mcp_hub().call("order", "create_after_sales_proposal", args, use_cache=False)

    # ------------------------------------------------------------ L3 组织记忆
    # 这三个工具都会碰同步 SQLite/MySQL 会话：一律丢进线程池（评审 v3-①），
    # 否则一次 30ms 的库写入就是一次事件循环停顿（SSE 心跳、其它会话都在等）。
    async def search_analyses(query: str, limit: int = 5) -> dict:
        """检索历史分析档案（跨会话复用结论）。只返回摘要，不含原对话全文，按部门隔离。"""

        def _run() -> list[dict]:
            with session_scope() as session:
                return org_memory.search_analyses(
                    session, query, departments=_scope_departments(user),
                    limit=max(1, min(limit, 10)),
                )

        items = await asyncio.to_thread(_run)
        matched = str((items[0].get("matched") if items else "") or "")
        note = ("关键词没有命中：下面是**最近的档案**（不是与查询相关的结论），"
                "回答时请说明这一点，不要当成匹配结果" if matched == "recent_fallback"
                else "按关键词相关度排序")
        # 复审 S1：把"关键词命中 / 最近档案兜底"显式告诉模型（以前这个区别是隐形的）
        return {"count": len(items), "items": items, "matched": matched, "note": note}

    async def read_analysis(analysis_id: str) -> dict:
        """读取一份分析档案全文（markdown）。跨部门返回无权限。"""

        def _run() -> dict | None:
            with session_scope() as session:
                return org_memory.get_analysis(session, analysis_id,
                                               departments=_scope_departments(user))

        data = await asyncio.to_thread(_run)
        return data or {"error": f"无权限或档案不存在：{analysis_id}"}

    async def save_analysis(question: str, conclusion: str, key_metrics: dict,
                            evidence: list[str], title: str = "") -> dict:
        """把本次结论沉淀为组织记忆（analysis_result + data/analyses/<部门>/*.md）。

        仅在用户明确要求「沉淀/留档/存档」时调用。同一结论重复沉淀不会产生第二份档案
        （按内容指纹判重，返回 deduped=true）。**档案部门按本轮取数的 SKU/款自动推断**：
        只有真正的跨部门结论才会落「全员」（全部门可见）。
        """
        payload_metrics = key_metrics if isinstance(key_metrics, dict) else {}
        payload_evidence = [str(x) for x in (evidence or [])][:20]
        session_id = current_tool_context().get("session_id", "")
        # 复审 F1：部门跟着**内容**走，而不是跟着"谁在问"走（admin 早先一律落「全员」，
        # 于是单部门分析会被全部门读到）。推断失败退回账号部门，绝不因"查不到"就公开。
        archive_dept, dept_source, dept_note = _infer_archive_department(
            user, question=question, conclusion=conclusion, evidence=payload_evidence,
        )
        if dept_note:
            logger.info("沉淀档案的部门为推断结果：%s（source=%s）", archive_dept or "全员",
                        dept_source)

        def _run() -> dict:
            with session_scope() as session:
                return org_memory.save_analysis(
                    session=session,
                    department=archive_dept,
                    username=user.username if user else "agent",
                    session_id=session_id,
                    question=question, conclusion=conclusion, title=title,
                    key_metrics=payload_metrics, evidence=payload_evidence,
                    metric_version=get_settings().metric_version,
                    model=get_settings().llm_model, source="chat", tags=[],
                )

        saved = await asyncio.to_thread(_run)
        saved["department_source"] = dept_source
        saved["department_note"] = dept_note
        if not saved.get("deduped"):
            # 主文档合并会留下「结论摘要待整合」的任务。默认**后台跑**（不阻塞本轮答复），
            # 需要同步等待时置 ANALYSIS_MERGE_INLINE=true（仍受超时约束）。
            settings_now = get_settings()
            if settings_now.analysis_merge_inline:
                try:
                    await asyncio.wait_for(
                        org_memory.adrain_pending_merges(limit=2),
                        timeout=float(settings_now.analysis_merge_timeout_seconds),
                    )
                except Exception:  # noqa: BLE001 —— 摘要整合失败留给离线脚本
                    pass
            else:
                _spawn_background(org_memory.adrain_pending_merges(limit=2))
        return saved

    # (fn, 对外工具名, 描述)。原先这里还有一列「分组」，但装配循环从未读它，
    # 已经和 TOOL_GROUPS 漂移过一次（list_pending_approvals 写着 write，实际在 approval 组）：
    # 分组归 TOOL_GROUPS、作用域归 TOOL_SCOPES，这里不再保留第二份真相。
    specs = [
        (get_inventory, "get_inventory", "查询 SKU 可用库存（治理口径）"),
        (get_sell_through, "get_sell_through", "查询 SKU 预计售罄天数与缺货风险等级"),
        (get_sku_metrics, "get_sku_metrics", "查询 SKU 治理指标：安全库存线/再订货点/在途/售罄"),
        (get_in_transit, "get_in_transit", "查询 SKU 在途采购与到货时间"),
        (get_reservations, "get_reservations", "查询 SKU 的 CRM 客户预订（脱敏）"),
        (get_orders, "get_orders", "查询 SKU 近 N 天订单与待发货占用"),
        (get_sales, "get_sales", "查询 SKU 销量与日均销量"),
        (get_metric_definitions, "get_metric_definitions", "获取数据中台治理口径元数据"),
        (get_sales_rate, "get_sales_rate", "查询动销率（部门账号自动限定本部门）"),
        (create_replenishment_order, "create_replenishment_order",
         "创建补货草稿单（从不自动审批：大额返回 pending_confirmation，需人工确认；"
         "同会话同轮次同参数自动去重，不要自己编幂等键）"),
        (create_transfer_suggestion, "create_transfer_suggestion",
         "查询跨仓调拨建议（会留下一条调拨建议记录，不改库存）"),
        (list_pending_approvals, "list_pending_approvals", "查询待人工确认的补货草稿单（仅本部门）"),
        # 服装口径：款级与尺码结构（SKU 级指标答不了断码/齐码/追单）
        (get_style_metrics, "get_style_metrics",
         "查询款级指标：生命周期/尺码曲线/齐码断码/周转/售罄"),
        (get_size_curve, "get_size_curve", "查询某款的尺码曲线（各码销量占比，识别主销码）"),
        (get_size_gap, "get_size_gap", "查询断码款清单（主销码缺货）与齐码率/断码率"),
        (get_apparel_overview, "get_apparel_overview",
         "服装经营概览：齐码率/断码率/平均周转/平均售罄/生命周期分布（部门级问题用它，别逐个 SKU 堆明细）"),
        (get_order_status, "get_order_status", "查询订单状态（含物流与售后工单）"),
        (list_orders, "list_orders", "查询某 SKU 近 N 天订单（到哪了/买了什么）"),
        (get_logistics, "get_logistics", "查询订单物流状态与预计到货"),
        (create_after_sales, "create_after_sales",
         "发起售后工单草稿（需人工确认，不会自动退款；同订单同原因数量自动去重）"),
        (list_memory_skills, "list_skills", "列出可用 Skills（操作流程清单）"),
        (load_skill, "load_skill",
         "按需加载某个 skill 的完整操作流程（需要成体系的排查/处置步骤时才用）"),
        (plan_query, "plan_query",
         "把问题解析成结构化查询计划（意图/实体/时间窗/置信度）；系统每轮对话前已自动解析，通常不必调用"),
        (search_knowledge, "search_knowledge",
         "检索知识库 SOP/规则/案例（制度/流程/阈值/案例类问题用它；库存销量等业务数字走取数工具；"
         "检索不到时返回 no_answer=true）"),
        (search_analyses, "search_analyses",
         "检索历史分析档案（用户提到「上次/之前的结论」时用，跨会话复用，部门隔离）"),
        (read_analysis, "read_analysis", "读取某份分析档案全文（先用 search_analyses 拿 analysis_id）"),
        (save_analysis, "save_analysis",
         "把本次结论沉淀为组织记忆档案（**仅在用户明确要求「沉淀/留档/存档」时调用**；"
         "同一结论重复沉淀会自动去重，但不要主动替用户留档）"),
    ]
    # 写操作白名单：读/写分离，默认只有 admin 能用写工具（部门用户只能看与申请）。
    # 这样「越权写」不再依赖提示词自觉——模型再想写也没有那个工具。
    write_roles = {r.strip().lower() for r in getattr(get_settings(), "write_tools_roles", [])}
    can_write = user is not None and (user.is_admin or (user.role or "").lower() in write_roles)

    # allow：注册表传入的「该 agent 可用工具」；None = 全量（默认行为不变）
    selected = resolve_tool_selection(allow)

    # 装配顺序（不要调换）：
    #   1) scope_guard 按 TOOL_SCOPES 声明做部门作用域校验（唯一执行点）；
    #   2) 写权限拒绝桩在外层：部门账号连「作用域判定」都不用做，直接拿到明确拒绝；
    #   3) with_result_contract 补齐 result_id/as_of/summary 并记台账；
    #   4) 最外层 with_tool_loop_guard：同一工具反复调用/连续失败/超预算 → 终止本轮（P1-1）。
    #      它必须最外层，才能看到契约封装后的 success/error；没有轮次上下文时完全惰性。
    tools = []
    for fn, name, desc in specs:
        if selected is not None and name not in selected:
            continue
        guarded = scope_guard(name, user)(fn)
        if name in WRITE_TOOL_NAMES and not can_write:
            guarded = _make_writer_denied(name, guarded)
            desc = desc + "（当前账号无写权限，只能申请/查看；如需执行请用管理员账号）"
        tools.append(
            StructuredTool.from_function(
                coroutine=with_tool_loop_guard(name)(with_result_contract(name)(guarded)),
                name=name,
                # 描述 + 参数说明都是**模型可见的提示词**：写工具的"何时该调/不该调"必须放这里，
                # 只写在函数 docstring 里模型是看不到的（复审 A1）。
                description=desc,
                args_schema=_tool_args_schema(fn, name),
            )
        )
    return tools
