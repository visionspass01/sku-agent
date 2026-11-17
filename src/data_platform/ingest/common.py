"""入湖同步公共工具：PII 脱敏 + 幂等覆盖写入。"""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session


def mask_name(name: str | None) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    if len(name) == 1:
        return name + "*"
    return name[0] + "*" * (len(name) - 1)


def mask_phone(phone: str | None) -> str:
    phone = (phone or "").strip()
    if len(phone) < 7:
        return "*" * len(phone)
    return f"{phone[:3]}****{phone[-4:]}"


def mask_receiver(receiver: str | None) -> str:
    """形如「张伟（杭州市）」：仅保留姓氏首字 + 区域。"""
    receiver = (receiver or "").strip()
    if "（" in receiver and receiver.endswith("）"):
        name, region = receiver.split("（", 1)
        return f"{mask_name(name)}（{region}"
    return mask_name(receiver)


def clear_table(session: Session, model) -> None:
    session.execute(delete(model))
