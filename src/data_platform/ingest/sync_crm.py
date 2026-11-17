"""CRM 接入：客户与预订记录入湖（入湖前完成姓名/手机号脱敏）。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from data_platform.db import RawCrmCustomer, RawCrmReservation
from data_platform.ingest.common import clear_table, mask_name, mask_phone
from data_platform.mock_sources import load_snapshot

_CUSTOMER_CACHE: dict[str, dict] = {}


def mask_customer(customer: dict) -> dict:
    """返回脱敏后的客户字段（原始 PII 不落库）。"""
    return {
        "customer_id": customer["customer_id"],
        "name_masked": mask_name(customer.get("name")),
        "phone_masked": mask_phone(customer.get("phone")),
        "tier": customer.get("tier", "普通"),
        "region": customer.get("region", ""),
    }


def sync_crm(session: Session, run_id: str = "manual") -> int:
    customers = load_snapshot("crm_customers.json")["items"]
    clear_table(session, RawCrmReservation)
    clear_table(session, RawCrmCustomer)
    _CUSTOMER_CACHE.clear()
    rows = 0
    for c in customers:
        masked = mask_customer(c)
        _CUSTOMER_CACHE[masked["customer_id"]] = masked
        session.add(RawCrmCustomer(**masked))
        rows += 1

    reservations = load_snapshot("crm_reservations.json")["items"]
    for r in reservations:
        session.add(RawCrmReservation(
            reservation_id=r["reservation_id"], sku_id=r["sku_id"],
            customer_id=r["customer_id"], qty=int(r.get("qty") or 0),
            reserved_at=dt.datetime.fromisoformat(str(r["reserved_at"])),
            expected_ship=dt.date.fromisoformat(str(r["expected_ship"])),
            status=r.get("status", "待发货"),
        ))
        rows += 1
    session.flush()
    return rows
