"""订单域接入：物流单 + 售后工单入湖。

为什么和 `sync_orders.py` 分开：订单是「交易事实」，物流/售后是「履约事实」，
在真实系统里分别来自订单中心与售后中心，接入失败的爆炸半径也不同——分开一个任务，
物流接口挂了不至于把订单表也清空。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from data_platform.db import RawAfterSalesTicket, RawLogistics
from data_platform.ingest.common import clear_table
from data_platform.mock_sources import load_snapshot


def sync_logistics(session: Session, run_id: str = "manual") -> int:
    rows = load_snapshot("logistics.json")["items"]
    clear_table(session, RawLogistics)
    for it in rows:
        eta = str(it.get("eta") or "")[:10]
        session.add(RawLogistics(
            order_id=it["order_id"], carrier=it.get("carrier", ""),
            tracking_no=it.get("tracking_no", ""), status=it.get("status", "已揽收"),
            eta=dt.date.fromisoformat(eta) if eta else None,
            updated_at=dt.datetime.fromisoformat(str(it.get("updated_at") or
                                                    dt.datetime.now().isoformat(timespec="seconds"))),
        ))
    session.flush()
    return len(rows)


def sync_after_sales(session: Session, run_id: str = "manual") -> int:
    rows = load_snapshot("after_sales.json")["items"]
    clear_table(session, RawAfterSalesTicket)
    for it in rows:
        key = f"{it['order_id']}|{it['reason']}|{int(it.get('qty') or 0)}"
        session.add(RawAfterSalesTicket(
            ticket_id=it["ticket_id"], order_id=it.get("order_id", ""),
            sku_id=it.get("sku_id", ""), reason=it.get("reason", ""),
            qty=int(it.get("qty") or 0), status=it.get("status", "pending_confirmation"),
            idempotency_key=key,
            created_at=dt.datetime.fromisoformat(str(it.get("created_at")
                                                     or dt.datetime.now().isoformat(timespec="seconds"))),
        ))
    session.flush()
    return len(rows)
