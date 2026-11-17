"""中台侧身份与作用域（治理下沉的核心）。

背景（评审 2.2-1 / 2.2-2 / 2.3）：早先中台只有一个共享 `INTERNAL_TOKEN`，`verify_token` 只做
字符串相等 —— 中台**不知道调用方是谁**，也就无法做「按 Agent 授权、按 Agent 限流、按 Agent 审计」，
部门隔离只能靠每个 Agent 自己实现一遍（新接一个 Agent 就要重写一遍权限）。

现在的模型：
- **令牌 → 主体（Principal）**：一个 key 对应一个 `agent_id`，带 `scopes` 与 `departments`；
- **scope**：`read` / `write` / `approve` / `etl` 四类能力，路由按需声明；
- **departments**：`None` = 全部门（管理员型 key），否则只能访问本部门 SKU；
- **向后兼容**：`PLATFORM_INTERNAL_TOKEN` 仍然有效，映射成全权的 `legacy-internal` 主体，
  既有部署/演示/测试零改动；多 Agent 靠新增 `PLATFORM_API_KEYS` 开启。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from fastapi import HTTPException, Request

from data_platform import config
from data_platform.config import department_of

logger = logging.getLogger("data-platform.authz")

SCOPES = frozenset({"read", "write", "approve", "etl", "admin"})
LEGACY_AGENT_ID = "legacy-internal"
TOKEN_HEADERS = ("X-Internal-Token", "X-Api-Key")
# 注册表快照的缓存时长：注册/改 scope 后最多这么久生效（写操作会主动 invalidate）
DB_CACHE_SECONDS = 5.0


class Principal:
    """一个调用方（Agent）的身份。"""

    __slots__ = ("agent_id", "scopes", "departments", "rate_limit", "source")

    def __init__(self, agent_id: str, scopes: frozenset[str],
                 departments: tuple[str, ...] | None = None, rate_limit: int = 0,
                 source: str = "env") -> None:
        self.agent_id = agent_id
        self.scopes = frozenset(scopes)
        self.departments = departments
        self.rate_limit = int(rate_limit or 0)
        # 身份来源：env（PLATFORM_API_KEYS）/ db（注册表）/ legacy（单一内部令牌）
        self.source = source

    # ------------------------------------------------------------ 判定
    def has(self, scope: str) -> bool:
        return scope in self.scopes

    def department_allowed(self, category: str | None) -> bool:
        """按 SKU 品类判断部门作用域；`departments=None` 表示不限制。"""
        if self.departments is None:
            return True
        dept = department_of(category)
        return dept is not None and dept in self.departments

    def to_public(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "scopes": sorted(self.scopes),
            "departments": list(self.departments) if self.departments else None,
            "source": self.source,
        }

    def __repr__(self) -> str:  # pragma: no cover —— 排障用
        return f"<Principal {self.agent_id} scopes={sorted(self.scopes)} deps={self.departments}>"


def hash_token(token: str) -> str:
    """令牌只以 sha256 落库：库被读走也拿不到可用的明文。"""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _parse_keys(raw: str) -> dict[str, Principal]:
    """解析 `PLATFORM_API_KEYS`：`{agent_id: {key, scopes, departments, rate_limit}}`。"""
    if not (raw or "").strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("PLATFORM_API_KEYS 不是合法 JSON，已忽略：%s", exc)
        return {}
    out: dict[str, Principal] = {}
    for agent_id, spec in (payload or {}).items():
        if not isinstance(spec, dict) or not spec.get("key"):
            logger.warning("PLATFORM_API_KEYS[%s] 缺少 key，已跳过", agent_id)
            continue
        scopes = {s for s in (spec.get("scopes") or ["read"]) if s in SCOPES}
        unknown = set(spec.get("scopes") or []) - SCOPES
        if unknown:
            logger.warning("PLATFORM_API_KEYS[%s] 有未知 scope %s（已忽略）", agent_id, sorted(unknown))
        departments = spec.get("departments")
        out[str(spec["key"])] = Principal(
            agent_id=str(agent_id),
            scopes=frozenset(scopes),
            departments=tuple(departments) if departments else None,
            rate_limit=int(spec.get("rate_limit") or config.RATE_LIMIT_PER_MINUTE),
            source="env",
        )
    return out


_PRINCIPALS: dict[str, Principal] | None = None
_DB_PRINCIPALS: dict[str, Principal] | None = None   # token_hash -> Principal
_DB_LOADED_AT: float = 0.0
_DB_ERROR: str = ""


def principals() -> dict[str, Principal]:
    """令牌 → 主体（环境变量版）。惰性构建并缓存；`reset_principals()` 供测试/改配置后重建。"""
    global _PRINCIPALS
    if _PRINCIPALS is None:
        _PRINCIPALS = _parse_keys(config.API_KEYS_RAW)
        # 兼容：单一内部令牌 = 全权主体（既有部署/演示不需要任何改动）
        legacy = Principal(LEGACY_AGENT_ID, SCOPES, None, config.RATE_LIMIT_PER_MINUTE,
                           source="legacy")
        _PRINCIPALS.setdefault(config.INTERNAL_TOKEN, legacy)
        if config.API_KEYS_RAW:
            logger.info("中台已配置 %d 个调用方身份", len(_PRINCIPALS))
    return _PRINCIPALS


def _load_db_principals(force: bool = False) -> dict[str, Principal]:
    """把注册表（platform_agent 表）读进内存快照。

    为什么要快照：认证发生在每个请求的中间件里，逐请求查库会在高并发下把中台自己拖慢。
    代价是「注册后最多 DB_CACHE_SECONDS 秒生效」——写操作会主动 invalidate，实际是立刻生效。
    """
    global _DB_PRINCIPALS, _DB_LOADED_AT, _DB_ERROR
    now = time.monotonic()
    if not force and _DB_PRINCIPALS is not None and now - _DB_LOADED_AT < DB_CACHE_SECONDS:
        return _DB_PRINCIPALS
    snapshot: dict[str, Principal] = {}
    try:
        from sqlalchemy import select

        from data_platform.db import PlatformAgent, session_scope

        with session_scope() as session:
            rows = session.scalars(select(PlatformAgent)).all()
            for row in rows:
                if (row.status or "active") != "active":
                    continue
                snapshot[row.token_hash] = Principal(
                    agent_id=row.agent_id,
                    scopes=frozenset(s for s in (row.scopes or []) if s in SCOPES),
                    departments=tuple(row.departments) if row.departments else None,
                    rate_limit=int(row.rate_limit or config.RATE_LIMIT_PER_MINUTE),
                    source="db",
                )
        _DB_ERROR = ""
    except Exception as exc:  # noqa: BLE001 —— 注册表读不到不能让整个中台认证崩掉
        _DB_ERROR = f"{type(exc).__name__}: {exc}"
        logger.warning("读取 Agent 注册表失败（沿用环境变量身份）：%s", _DB_ERROR)
        snapshot = _DB_PRINCIPALS or {}
    _DB_PRINCIPALS = snapshot
    _DB_LOADED_AT = now
    return snapshot


def registered_agents() -> dict[str, Principal]:
    """当前注册表中的主体（供 /agents 列表与测试断言）。"""
    return dict(_load_db_principals())


def registry_status() -> dict:
    snapshot = _load_db_principals()
    return {
        "env_principals": max(0, len(principals()) - 1),   # 去掉 legacy
        "db_principals": len(snapshot),
        "cache_seconds": DB_CACHE_SECONDS,
        "error": _DB_ERROR,
        "note": "环境变量身份（PLATFORM_API_KEYS）+ 注册表身份（/api/v1/agents）合并生效",
    }


def lookup(token: str) -> Principal | None:
    """令牌 → 主体（环境变量优先，其次注册表，最后兼容单一内部令牌）。"""
    if not token:
        return None
    found = principals().get(token)
    if found is not None:
        return found
    from_db = _load_db_principals().get(hash_token(token))
    if from_db is not None:
        return from_db
    return None


def reset_principals() -> None:
    global _PRINCIPALS, _DB_PRINCIPALS, _DB_LOADED_AT
    _PRINCIPALS = None
    _DB_PRINCIPALS = None
    _DB_LOADED_AT = 0.0


def invalidate_registry() -> None:
    """注册表写入后调用：下一次认证立即重建快照（不等 TTL）。"""
    global _DB_LOADED_AT
    _DB_LOADED_AT = 0.0


# ---------------------------------------------------------------- 解析 / 路由依赖
def token_of(request: Request) -> str:
    for header in TOKEN_HEADERS:
        value = request.headers.get(header, "")
        if value:
            return value.strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def resolve_principal(request: Request) -> Principal | None:
    """从请求头解析主体；解析成功会顺手写入 `request.state`（供审计与路由复用）。"""
    token = token_of(request)
    if not token:
        return None
    principal = lookup(token)
    if principal is not None:
        request.state.principal = principal
    return principal


def current_principal(request: Request) -> Principal:
    """路由依赖：拿不到主体就 401（中间件已先拦，这里是纵深）。"""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        principal = resolve_principal(request)
    if principal is None:
        raise HTTPException(401, "缺少或无效的中台调用令牌（X-Internal-Token / X-Api-Key）")
    return principal


def require_scope(scope: str):
    """路由依赖：声明该端点需要的能力（read / write / approve / etl）。"""
    if scope not in SCOPES:
        raise RuntimeError(f"未知 scope：{scope}")

    def dependency(request: Request) -> Principal:
        principal = current_principal(request)
        if not principal.has(scope):
            request.state.audit_decision = "denied_scope"
            request.state.audit_detail = f"缺少 scope={scope}（当前 {sorted(principal.scopes)}）"
            raise HTTPException(
                403,
                f"调用方 {principal.agent_id} 缺少 scope={scope}；"
                f"当前权限 {sorted(principal.scopes)}",
            )
        return principal

    return dependency


def ensure_department(principal: Principal, category: str | None, request: Request | None = None,
                      sku_id: str = "") -> None:
    """部门作用域校验：越权 → 403（中台是边界，要可诊断，所以不用 404）。"""
    if principal.department_allowed(category):
        return
    if request is not None:
        request.state.audit_decision = "denied_department"
        request.state.audit_detail = f"sku={sku_id} category={category}"
    raise HTTPException(
        403,
        f"调用方 {principal.agent_id} 只能访问 {list(principal.departments or [])} 部门数据；"
        f"该 SKU 属于 {department_of(category) or '未知'}（品类 {category or '未标注'}）",
    )


def department_scope(principal: Principal) -> tuple[str, ...] | None:
    return principal.departments
