"""订单库接入：订单明细入湖（收货人信息脱敏）。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from data_platform.db import RawOrder
from data_platform.ingest.common import clear_table, mask_receiver
from data_platform.mock_sources import load_snapshot


def sync_orders(session: Session, run_id: str = "manual") -> int:
    orders = load_snapshot("orders.json")["items"]
    clear_table(session, RawOrder)
    for o in orders:
        session.add(RawOrder(
            order_id=o["order_id"], sku_id=o["sku_id"],
            customer_id=o.get("customer_id", ""), qty=int(o.get("qty") or 0),
            amount=float(o.get("amount") or 0),
            order_date=dt.date.fromisoformat(str(o["order_date"])[:10]),
            status=o.get("status", "已发货"),
            receiver_masked=mask_receiver(o.get("receiver")),
        ))
    session.flush()
    return len(orders)
