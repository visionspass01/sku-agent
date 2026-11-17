"""ERP 接入：库存快照与在途采购入湖。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from data_platform.db import RawErpInTransit, RawErpInventory
from data_platform.ingest.common import clear_table
from data_platform.mock_sources import load_snapshot


def sync_erp(session: Session, run_id: str = "manual") -> int:
    inv = load_snapshot("erp_inventory.json")
    snapshot_date = dt.date.fromisoformat(inv["meta"]["snapshot_date"])
    clear_table(session, RawErpInventory)
    rows = 0
    for it in inv["items"]:
        session.add(RawErpInventory(
            run_id=run_id,
            snapshot_date=snapshot_date,
            sku_id=it["sku_id"], style_id=it.get("style_id", ""),
            style_name=it.get("style_name", ""), category=it.get("category", ""),
            season=it.get("season", ""), color=it.get("color", ""), size=it.get("size", ""),
            warehouse_id=it.get("warehouse_id", ""), price=float(it.get("price") or 0),
            platform=it.get("platform", ""),
            list_price=float(it.get("list_price") or it.get("price") or 0),
            launch_days_ago=int(it.get("launch_days_ago") or 120),
            on_hand=int(it.get("on_hand") or 0), locked=int(it.get("locked") or 0),
            return_rate=float(it.get("return_rate") or 0),
            supplier_id=it.get("supplier_id", ""), lead_time_days=int(it.get("lead_time_days") or 10),
        ))
        rows += 1

    transit = load_snapshot("erp_in_transit.json")
    clear_table(session, RawErpInTransit)
    for it in transit["items"]:
        session.add(RawErpInTransit(
            run_id=run_id, snapshot_date=snapshot_date,
            po_no=it.get("po_no", ""), sku_id=it["sku_id"], qty=int(it.get("qty") or 0),
            eta=dt.date.fromisoformat(str(it["eta"])[:10]),
            supplier_id=it.get("supplier_id", ""), status=it.get("status", "在途"),
        ))
        rows += 1
    session.flush()
    return rows
