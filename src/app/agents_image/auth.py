"""独立账号的密码与令牌实现（复刻 SKU 的语义，但**不 import 它的 auth 模块**）。

- 密码：`sha256(随机盐 + 明文)`，每用户独立盐；
- 令牌：`base64url(payload).base64url(hmac_sha256(payload))`，payload = {sub, role, dept, exp, jti}；
- 登录限流：进程内滑动窗口（用户名 + IP 维度），与 SKU 同思路（重启即清零，演示够用）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from app.agents_image.config import get_image_settings
from app.core.errors import ImageError, raise_image

_SALT_BYTES = 16


def new_salt() -> str:
    return secrets.token_hex(_SALT_BYTES)


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or new_salt()
    digest = hashlib.sha256((salt + (password or "")).encode("utf-8")).hexdigest()
    return digest, salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    digest, _ = hash_password(password, salt)
    return hmac.compare_digest(digest, password_hash or "")


@dataclass
class AuthUser:
    username: str
    role: str = "user"
    department: str | None = None
    jti: str = ""
    exp: float = 0.0

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue_token(user: AuthUser, *, ttl_hours: int | None = None,
                secret: str | None = None, now: float | None = None) -> str:
    settings = get_image_settings()
    secret = secret or settings.auth_secret
    ttl = ttl_hours or settings.token_ttl_hours
    issued = float(now if now is not None else time.time())
    payload = {
        "sub": user.username,
        "role": user.role,
        "dept": user.department,
        "exp": issued + ttl * 3600,
        "jti": secrets.token_hex(8),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    signature = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                              hashlib.sha256).digest())
    return f"{body}.{signature}"


def parse_token(token: str, *, secret: str | None = None, now: float | None = None) -> AuthUser:
    settings = get_image_settings()
    secret = secret or settings.auth_secret
    try:
        body, signature = token.split(".", 1)
    except ValueError as exc:
        raise ImageError("IMG_UNAUTHORIZED", "令牌格式不正确") from exc
    expected = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise_image("IMG_UNAUTHORIZED", "令牌签名不匹配")
    payload = json.loads(_unb64(body))
    if float(payload.get("exp") or 0) < float(now if now is not None else time.time()):
        raise_image("IMG_UNAUTHORIZED", "令牌已过期")
    return AuthUser(username=str(payload.get("sub") or ""), role=str(payload.get("role") or "user"),
                    department=payload.get("dept"), jti=str(payload.get("jti") or ""),
                    exp=float(payload.get("exp") or 0))


def bearer_from_header(value: str | None) -> str:
    if not value:
        raise_image("IMG_UNAUTHORIZED", "缺少 Authorization: Bearer <token>")
    parts = value.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise_image("IMG_UNAUTHORIZED", "Authorization 头格式应为 Bearer <token>")
    return parts[1].strip()


# ---------------------------------------------------------------- 登录限流
@dataclass
class LoginThrottle:
    max_attempts: int = 8
    window_seconds: int = 300
    _failures: dict = field(default_factory=lambda: defaultdict(deque))

    def _key(self, username: str, ip: str) -> tuple[str, str]:
        return (username or "", ip or "")

    def allowed(self, username: str, ip: str, now: float | None = None) -> tuple[bool, int]:
        moment = float(now if now is not None else time.time())
        bucket = self._failures[self._key(username, ip)]
        while bucket and moment - bucket[0] > self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.max_attempts:
            retry_after = max(1, int(self.window_seconds - (moment - bucket[0])))
            return False, retry_after
        return True, 0

    def record_failure(self, username: str, ip: str, now: float | None = None) -> None:
        moment = float(now if now is not None else time.time())
        self._failures[self._key(username, ip)].append(moment)

    def reset(self, username: str | None = None, ip: str | None = None) -> None:
        if username is None and ip is None:
            self._failures.clear()
            return
        self._failures.pop((username or "", ip or ""), None)


_THROTTLE = LoginThrottle()


def get_throttle() -> LoginThrottle:
    settings = get_image_settings()
    _THROTTLE.max_attempts = settings.login_max_attempts
    _THROTTLE.window_seconds = settings.login_window_seconds
    return _THROTTLE


def reset_throttle() -> None:
    _THROTTLE.reset()
