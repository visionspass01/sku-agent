"""认证与授权（演示版）。

- 账号固定为演示账号：admin / worker1（男装部门） / worker2（女装部门）；
- 密码使用 sha256(盐 + 明文) 存储，避免明文落库；
- 登录成功签发 HMAC 签名 Token（payload: 用户名/角色/部门/过期时间）；
- 登出加入内存黑名单（进程重启失效）；
- 安全记录：登录时记录 用户名/IP/时间，仅保留最近 N 小时（默认 4 小时）。
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.security.throttle import SlidingWindowCounter

ROLE_ADMIN = "admin"
ROLE_USER = "user"

_PASSWORD_SALT = "sku-demo-salt::"

# 演示账号（生产环境请改为企业 SSO / 数据库账号表）
DEMO_ACCOUNTS: dict[str, dict] = {
    "admin": {"role": ROLE_ADMIN, "department": None, "password": "hhw123"},
    "worker1": {"role": ROLE_USER, "department": "男装", "password": "hhw111"},
    "worker2": {"role": ROLE_USER, "department": "女装", "password": "hhw222"},
}

_PASSWORD_HASHES = {
    username: hashlib.sha256((_PASSWORD_SALT + spec["password"]).encode("utf-8")).hexdigest()
    for username, spec in DEMO_ACCOUNTS.items()
}

# Token 黑名单：token -> 过期时间（进程内）
_REVOKED: dict[str, float] = {}


@dataclass(frozen=True)
class AuthUser:
    username: str
    role: str
    department: str | None

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def to_dict(self) -> dict:
        return {"username": self.username, "role": self.role, "department": self.department}


def authenticate(username: str, password: str) -> AuthUser | None:
    expected = _PASSWORD_HASHES.get(username)
    if not expected:
        return None
    actual = hashlib.sha256((_PASSWORD_SALT + password).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(expected, actual):
        return None
    spec = DEMO_ACCOUNTS[username]
    return AuthUser(username=username, role=spec["role"], department=spec["department"])


# ---------------------------------------------------------------- Token
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload_b64: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()


def issue_token(user: AuthUser) -> str:
    settings = get_settings()
    exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=settings.auth_token_ttl_hours)
    payload = json.dumps(
        {"u": user.username, "r": user.role, "d": user.department, "e": exp.timestamp()},
        ensure_ascii=False, separators=(",", ":"),
    )
    body = _b64(payload.encode("utf-8"))
    return f"{body}.{_sign(body, settings.auth_secret)}"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def revoke_token(token: str) -> None:
    """注销 token：写进持久化黑名单（SQLite），重启后仍拒绝；Redis 配置了再加一层 TTL 镜像。"""
    exp_ts = 0.0
    try:
        body, _sig = token.split(".", 1)
        data = json.loads(_unb64(body))
        exp_ts = float(data.get("e", 0))
        _REVOKED[token] = exp_ts  # 进程内快路径
    except Exception:
        pass
    if not exp_ts:
        return
    try:
        from app.db.engine import session_scope
        from app.db.models import RevokedToken

        with session_scope() as session:
            key = _token_hash(token)
            row = session.get(RevokedToken, key)
            if row is None:
                session.add(RevokedToken(
                    token_hash=key,
                    exp=dt.datetime.fromtimestamp(exp_ts),
                    revoked_at=dt.datetime.now(),
                ))
            session.query(RevokedToken).filter(
                RevokedToken.exp < dt.datetime.now()
            ).delete(synchronize_session=False)
    except Exception:  # noqa: BLE001 —— 落库失败时仍有进程内黑名单
        pass
    try:  # 可选：Redis 镜像（配了真实 Redis 时，多进程也能立刻生效）
        settings = get_settings()
        if settings.redis_url:
            from app.cache.redis_factory import get_redis

            ttl = max(1, int(exp_ts - dt.datetime.now(dt.timezone.utc).timestamp()))
            get_redis().setex(f"auth:revoked:{_token_hash(token)}", ttl, "1")
    except Exception:  # noqa: BLE001
        pass
    _prune_revoked()


# 未撤销结果的短 TTL 缓存：token -> (检查时刻, 是否已撤销)
# 为什么缓存"未撤销"：鉴权发生在每个请求上，而这几乎总是 false，没必要每请求查库；
# 为什么安全：登出会先写进程内 `_REVOKED`，命中快路径立即失效，不走这个缓存。
_revoked_cache: dict[str, tuple[float, bool]] = {}
_REVOKED_CACHE_MAX = 5000


def _lookup_revoked_in_db(token: str) -> bool:
    try:
        from app.db.engine import session_scope
        from app.db.models import RevokedToken

        with session_scope() as session:
            row = session.get(RevokedToken, _token_hash(token))
            if row is not None and row.exp > dt.datetime.now():
                _REVOKED[token] = row.exp.timestamp()
                return True
    except Exception:  # noqa: BLE001 —— 读失败按未注销处理（进程内黑名单仍有效）
        pass
    return False


def is_revoked(token: str) -> bool:
    if token in _REVOKED:          # 进程内黑名单：登出立即生效，优先级最高
        return True
    ttl = max(0.0, float(getattr(get_settings(), "revocation_cache_seconds", 30.0)))
    now = time.monotonic()
    cached = _revoked_cache.get(token)
    if cached is not None and now - cached[0] < ttl:
        return cached[1]
    revoked = _lookup_revoked_in_db(token)
    if len(_revoked_cache) >= _REVOKED_CACHE_MAX:
        stale = [k for k, (at, _v) in _revoked_cache.items() if now - at >= ttl]
        for key in stale:
            _revoked_cache.pop(key, None)
        if len(_revoked_cache) >= _REVOKED_CACHE_MAX:
            _revoked_cache.clear()
    _revoked_cache[token] = (now, revoked)
    return revoked


def reset_revocation_cache() -> None:
    """测试用：清掉「未撤销」缓存与进程内黑名单。"""
    _revoked_cache.clear()
    _REVOKED.clear()


def _prune_revoked() -> None:
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    for token in [t for t, exp in _REVOKED.items() if exp < now]:
        _REVOKED.pop(token, None)


def decode_token(token: str) -> AuthUser:
    settings = get_settings()
    _prune_revoked()
    if is_revoked(token):
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _sign(body, settings.auth_secret)):
            raise ValueError("bad signature")
        data = json.loads(_unb64(body))
        if float(data.get("e", 0)) < dt.datetime.now(dt.timezone.utc).timestamp():
            raise ValueError("expired")
        return AuthUser(username=data["u"], role=data["r"], department=data.get("d"))
    except Exception:
        raise HTTPException(status_code=401, detail="未登录或登录已失效，请先登录账号") from None


def extract_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def require_user(request: Request) -> AuthUser:
    token = extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="未登录或登录已失效，请先登录账号")
    return decode_token(token)


def optional_user(request: Request) -> AuthUser | None:
    """读取接口用：带合法 Token 则返回用户，否则按匿名只读处理（不报 401）。"""
    token = extract_token(request)
    if not token:
        return None
    try:
        return decode_token(token)
    except HTTPException:
        return None


def require_admin(user: AuthUser = Depends(require_user)) -> AuthUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def scope_categories(user: AuthUser | None) -> list[str] | None:
    """数据域：匿名/管理员返回 None（全部只读数据），部门用户返回该部门品类。"""
    from app.config import categories_for

    if user is None or user.is_admin:
        return None
    return categories_for(user.department)


# ---------------------------------------------------------------- 登录限流
# 为什么要有：3 个演示账号 + 固定密码，没有限流就等于把「爆破」写进设计里。
# 策略（滑动窗口，进程内计数；多副本部署时把 SlidingWindowCounter 换成 Redis 实现即可）：
#   同一 (账号, IP) 在 LOGIN_RATE_WINDOW_SECONDS 内失败达到 LOGIN_RATE_LIMIT 次 → 429；
#   成功后清空计数，避免正常用户被自己之前的错输拖住。
_login_counter = SlidingWindowCounter()


def _login_key(username: str, ip: str) -> str:
    return f"{username}|{ip or '-'}"


def login_throttle_state(username: str, ip: str) -> dict:
    """返回 {allowed, remaining, retry_after_s, count, limit, window_s}。"""
    settings = get_settings()
    return _login_counter.state(
        _login_key(username, ip),
        limit=max(1, int(settings.login_rate_limit)),
        window=float(settings.login_rate_window_seconds),
    )


def record_login_failure(username: str, ip: str) -> None:
    settings = get_settings()
    _login_counter.hit(
        _login_key(username, ip), window=float(settings.login_rate_window_seconds)
    )


def clear_login_failures(username: str, ip: str) -> None:
    _login_counter.reset(_login_key(username, ip))


def reset_login_throttle() -> None:
    """测试用。"""
    _login_counter.reset()


# ---------------------------------------------------------------- 安全记录
def record_login(session: Session, username: str, ip: str, user_agent: str = "",
                 result: str = "ok") -> None:
    from app.db.models import AuthEvent

    now = dt.datetime.now()
    session.add(AuthEvent(
        username=username, ip=ip or "-", at=now,
        user_agent=(user_agent or "")[:200], result=result,
    ))
    prune_security_records(session)
    session.flush()


def prune_security_records(session: Session) -> None:
    from app.db.models import AuthEvent

    hours = get_settings().security_record_window_hours
    cutoff = dt.datetime.now() - dt.timedelta(hours=hours)
    session.execute(delete(AuthEvent).where(AuthEvent.at < cutoff))


def security_records(session: Session, username: str | None = None) -> list[dict]:
    """返回最近 N 小时登录记录；username=None 表示管理员查看全部账号。"""
    from app.db.models import AuthEvent

    hours = get_settings().security_record_window_hours
    cutoff = dt.datetime.now() - dt.timedelta(hours=hours)
    stmt = select(AuthEvent).where(AuthEvent.at >= cutoff).order_by(AuthEvent.at.desc())
    if username:
        stmt = stmt.where(AuthEvent.username == username)
    return [
        {
            "username": e.username,
            "ip": e.ip,
            "at": e.at.isoformat(timespec="seconds"),
            "result": getattr(e, "result", "ok") or "ok",
        }
        for e in session.scalars(stmt)
    ]
