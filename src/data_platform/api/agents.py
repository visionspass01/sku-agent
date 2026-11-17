"""Agent 注册与发现（多 Agent 插拔的中台侧落地）。

问题（评审 C2 / 2.1-2）：新 Agent 接入原先只能改 `PLATFORM_API_KEYS` 环境变量并重启中台，
"插拔"实际上是一次发布动作；而且注册表只有中台自己知道，调用方拿不到自己的权限清单。

现在的契约：
- `POST   /api/v1/agents`                 注册（需要 admin scope），**明文令牌只返回一次**
- `GET    /api/v1/agents`                 列表（read；只回 scopes/departments，永不回令牌）
- `GET    /api/v1/agents/me`              我是什么身份（任何已认证调用方；Agent 启动自检用）
- `PATCH  /api/v1/agents/{id}`            改 scopes / departments / rate_limit / status
- `POST   /api/v1/agents/{id}/rotate`     轮换令牌（旧的立刻失效）
- `POST   /api/v1/agents/{id}/disable`    禁用（等价于吊销）

安全取舍：令牌只存 sha256；明文只在注册与轮换的响应里出现一次。写操作会立即失效认证快照，
所以「注册完马上就能调」是确定的（不依赖 5s 缓存 TTL）。
"""

from __future__ import annotations

import datetime as dt
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select

from data_platform.api import authz
from data_platform.api import schemas as sc
from data_platform.api.authz import Principal, current_principal, hash_token, require_scope
from data_platform.config import DEPARTMENT_CATEGORIES, RATE_LIMIT_PER_MINUTE
from data_platform.db import PlatformAgent, session_scope

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

_AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
VALID_STATUS = ("active", "disabled")


def _new_token() -> str:
    return f"k-{secrets.token_urlsafe(24)}"


def _agent_payload(row: PlatformAgent, *, last_seen: str | None = None) -> dict:
    return {
        "agent_id": row.agent_id,
        "name": row.name or "",
        "scopes": sorted(row.scopes or []),
        "departments": list(row.departments) if row.departments else None,
        "rate_limit": int(row.rate_limit or 0),
        "status": row.status or "active",
        "contract_version": row.contract_version or "",
        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
        "rotated_at": row.rotated_at.isoformat(timespec="seconds") if row.rotated_at else None,
        "last_seen_at": last_seen,
        "source": "db",
    }


def _validate_spec(payload: dict, *, partial: bool = False) -> dict:
    """校验注册/更新参数。**未知 scope 直接报错**而不是静默丢弃（静默丢 = 权限比预期小）。"""
    out: dict = {}
    if "agent_id" in payload or not partial:
        agent_id = str(payload.get("agent_id") or "").strip()
        if not _AGENT_ID_RE.match(agent_id):
            raise HTTPException(400, "agent_id 需为 2~32 位小写字母开头，仅含 a-z0-9_-")
        out["agent_id"] = agent_id
    if "name" in payload or not partial:
        out["name"] = str(payload.get("name") or "")[:64]
    if "scopes" in payload or not partial:
        scopes = payload.get("scopes") or ["read"]
        if not isinstance(scopes, list):
            raise HTTPException(400, "scopes 必须是数组")
        unknown = sorted(set(scopes) - authz.SCOPES)
        if unknown:
            raise HTTPException(
                400, f"未知 scope {unknown}；可用 {sorted(authz.SCOPES)}")
        out["scopes"] = sorted(set(scopes))
    if "departments" in payload or not partial:
        departments = payload.get("departments")
        if departments is not None:
            if not isinstance(departments, list):
                raise HTTPException(400, "departments 必须是数组或 null")
            unknown = sorted(set(departments) - set(DEPARTMENT_CATEGORIES))
            if unknown:
                raise HTTPException(
                    400, f"未知部门 {unknown}；可用 {sorted(DEPARTMENT_CATEGORIES)}")
            out["departments"] = sorted(set(departments)) or None
        else:
            out["departments"] = None
    if "rate_limit" in payload or not partial:
        limit = int(payload.get("rate_limit") or RATE_LIMIT_PER_MINUTE)
        if limit <= 0:
            raise HTTPException(400, "rate_limit 必须 > 0（0 会造成该 Agent 永远 429）")
        out["rate_limit"] = limit
    if "status" in payload:
        status = str(payload.get("status") or "active")
        if status not in VALID_STATUS:
            raise HTTPException(400, f"status 只能是 {list(VALID_STATUS)}")
        out["status"] = status
    if "contract_version" in payload:
        out["contract_version"] = str(payload.get("contract_version") or "")[:16]
    return out


def _last_seen_map() -> dict[str, str]:
    """最近一次调用时间：直接取审计表里每个 agent_id 的最大时间（不额外写库）。"""
    from data_platform.db import ApiAuditLog

    try:
        with session_scope() as session:
            rows = session.execute(
                select(ApiAuditLog.agent_id, func.max(ApiAuditLog.at)).group_by(ApiAuditLog.agent_id)
            ).all()
        return {agent: at.isoformat(timespec="seconds") for agent, at in rows if agent and at}
    except Exception:  # noqa: BLE001 —— 审计表不可用时只是少一个展示字段
        return {}


def _get(session, agent_id: str) -> PlatformAgent:  # noqa: ANN001
    row = session.get(PlatformAgent, agent_id)
    if row is None:
        raise HTTPException(404, f"未注册的 Agent：{agent_id}")
    return row


# ---------------------------------------------------------------- 注册 / 列表 / me
@router.post("", response_model=sc.AgentRegisterOut,
             dependencies=[Depends(require_scope("admin"))])
def register_agent(payload: dict, request: Request,
                   principal: Principal = Depends(current_principal)) -> dict:
    spec = _validate_spec(payload or {})
    token = _new_token()
    row = PlatformAgent(
        agent_id=spec["agent_id"], name=spec.get("name", ""),
        token_hash=hash_token(token),
        scopes=spec.get("scopes") or ["read"],
        departments=spec.get("departments"),
        rate_limit=int(spec.get("rate_limit") or RATE_LIMIT_PER_MINUTE),
        status="active",
        contract_version=spec.get("contract_version", ""),
        created_at=dt.datetime.now(),
    )
    with session_scope() as session:
        if session.get(PlatformAgent, row.agent_id) is not None:
            raise HTTPException(409, f"Agent 已存在：{row.agent_id}（如需改权限用 PATCH）")
        session.add(row)
        session.flush()
        # 在事务内取字段：出事务后 ORM 实例会 detach/expire，再读属性会 DetachedInstanceError
        out = {"agent_id": row.agent_id, "scopes": sorted(row.scopes or []),
               "departments": list(row.departments) if row.departments else None,
               "rate_limit": int(row.rate_limit or 0),
               "contract_version": row.contract_version or ""}
    authz.invalidate_registry()
    return {
        **out,
        "token": token,
        "note": ("明文令牌只在注册/轮换时返回一次，请立刻写进该 Agent 的配置；"
                 f"注册者 {principal.agent_id} 已记入调用审计"),
    }


@router.get("", response_model=sc.AgentsOut, dependencies=[Depends(require_scope("read"))])
def list_agents(status: str = Query("", description="active / disabled / 空=全部")) -> dict:
    seen = _last_seen_map()
    with session_scope() as session:
        stmt = select(PlatformAgent).order_by(PlatformAgent.agent_id)
        if status:
            stmt = stmt.where(PlatformAgent.status == status)
        rows = session.scalars(stmt).all()
        items = [_agent_payload(row, last_seen=seen.get(row.agent_id)) for row in rows]
    # 环境变量身份也列出来：否则运维会以为「中台只有注册表里那几个 Agent」
    env_items = [p.to_public() for p in authz.principals().values()]
    for item in env_items:
        item.setdefault("last_seen_at", seen.get(item["agent_id"]))
    return {
        "count": len(items),
        "items": items,
        "env_principals": env_items,
        "registry": authz.registry_status(),
        "note": "注册表身份 + 环境变量身份（PLATFORM_API_KEYS）合并生效",
    }


@router.get("/me", response_model=sc.AgentOut)
def me(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    """我是谁：Agent 启动时自检用（自己的 scopes / departments / 能力目录版本）。"""
    seen = _last_seen_map()
    row = None
    try:
        with session_scope() as session:
            row = session.get(PlatformAgent, principal.agent_id)
            if row is not None:
                return _agent_payload(row, last_seen=seen.get(principal.agent_id))
    except Exception:  # noqa: BLE001 —— 老库没有注册表时，回落到"环境变量身份"即可
        row = None
    public = principal.to_public()
    return {
        **public,
        "status": "active",
        "rate_limit": principal.rate_limit,
        "contract_version": "",
        "created_at": None,
        "rotated_at": None,
        "last_seen_at": seen.get(principal.agent_id),
    }


# ---------------------------------------------------------------- 变更
@router.patch("/{agent_id}", response_model=sc.AgentOut,
              dependencies=[Depends(require_scope("admin"))])
def patch_agent(agent_id: str, payload: dict) -> dict:
    spec = _validate_spec(payload or {}, partial=True)
    with session_scope() as session:
        row = _get(session, agent_id)
        for field in ("name", "scopes", "departments", "rate_limit", "status", "contract_version"):
            if field in spec:
                setattr(row, field, spec[field])
        out = _agent_payload(row)
    authz.invalidate_registry()
    out["last_seen_at"] = _last_seen_map().get(agent_id)
    return out


@router.post("/{agent_id}/rotate", response_model=sc.AgentRegisterOut,
             dependencies=[Depends(require_scope("admin"))])
def rotate_token(agent_id: str) -> dict:
    token = _new_token()
    with session_scope() as session:
        row = _get(session, agent_id)
        row.token_hash = hash_token(token)
        row.rotated_at = dt.datetime.now()
        out = {"agent_id": row.agent_id, "scopes": sorted(row.scopes or []),
               "departments": list(row.departments) if row.departments else None,
               "rate_limit": int(row.rate_limit or 0),
               "contract_version": row.contract_version or ""}
    authz.invalidate_registry()
    return {**out, "token": token,
            "note": "旧令牌立即失效；新令牌只返回这一次"}


@router.post("/{agent_id}/disable", response_model=sc.AgentOut,
             dependencies=[Depends(require_scope("admin"))])
def disable_agent(agent_id: str) -> dict:
    with session_scope() as session:
        row = _get(session, agent_id)
        row.status = "disabled"
        out = _agent_payload(row)
    authz.invalidate_registry()
    return {**out, "status": "disabled"}
