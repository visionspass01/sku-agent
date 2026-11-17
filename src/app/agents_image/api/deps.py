"""生图 API 的依赖：当前以「可选用户」占位，D4 换成独立账号令牌校验（签名不变）。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request


@dataclass
class CurrentUser:
    username: str
    role: str = "user"
    department: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def current_user_optional(request: Request) -> CurrentUser | None:
    """D2–D3 阶段：还没有账号体系，返回 None（任务按 anonymous 落库）。

    D4 会改为「读 Authorization: Bearer <token> → 校验 → 返回 CurrentUser」，
    调用方（routes）不感知变化。
    """
    return None


def trace_id_of(request: Request) -> str:
    """优先用调用方带来的 X-Trace-Id，便于跨服务串联；没有就新生成一个短 id。"""
    import uuid

    return request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:12]
