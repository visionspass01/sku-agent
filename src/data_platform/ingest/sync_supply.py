"""供应链域接入：供应商主数据 + 仓间在途调拨。

供应商交期/MOQ/准时率是补货量的直接输入；调拨单回答「主仓缺货时能不能从别的仓补」。
这两者原先由 ERP MCP 直连 Agent 私有库提供，现在统一由中台持有。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from data_platform.db import RawSupplier, RawTransferOrder
from data_platform.ingest.common import clear_table
from data_platform.mock_sources import load_snapshot


def sync_supply(session: Session, run_id: str = "manual") -> int:
    clear_table(session, RawSupplier)
    suppliers = load_snapshot("erp_suppliers.json")["items"]
    for it in suppliers:
        session.add(RawSupplier(
            supplier_id=it["supplier_id"], name=it.get("name", ""),
            lead_time_days=int(it.get("lead_time_days") or 10),
            moq=int(it.get("moq") or 0),
            on_time_rate=float(it.get("on_time_rate") or 0),
        ))

    clear_table(session, RawTransferOrder)
    transfers = load_snapshot("transfers.json")["items"]
    for it in transfers:
        session.add(RawTransferOrder(
            transfer_no=it.get("transfer_no", ""), sku_id=it["sku_id"],
            from_warehouse_id=it.get("from_warehouse_id", ""),
            to_warehouse_id=it.get("to_warehouse_id", ""),
            qty=int(it.get("qty") or 0),
            eta=dt.date.fromisoformat(str(it["eta"])[:10]),
            status=it.get("status", "在途"),
        ))
    session.flush()
    return len(suppliers) + len(transfers)
