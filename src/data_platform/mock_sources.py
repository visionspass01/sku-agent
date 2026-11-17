"""Mock 数据源生成：模拟 ERP（库存/在途）、CRM（客户/预订）、订单库快照。

数据来源有两条路（默认第一条，**不依赖 Agent**）：
1. `data_platform.source_spec` 自举：中台自带的演示目录 + 确定性合成（推荐，容器/CI 都可用）；
2. `PLATFORM_SOURCE_DB=<path>`：显式指定时复用 Agent 演示库（本地便利路径；容器里通常没有这个文件，
   所以绝不能是默认行为——早先默认指向 `data/sku_demo.db` 导致 compose 里中台起不来 / mart 空表）。

CRM 客户/预订与订单明细为确定性合成（固定 seed），并保留原始姓名/手机号——脱敏在入湖时执行，
用于演示「接入层脱敏」。
"""

from __future__ import annotations

import datetime as dt
import json
import random
import sqlite3
from pathlib import Path

from data_platform.config import RAW_DIR, SOURCE_DB
from data_platform.source_spec import (
    AFTER_SALES_REASON_WEIGHT,
    CARRIERS,
    COMPETITOR_SHOPS,
    LOGISTICS_STATES,
    PLATFORM_ACTIVITIES,
    STYLES,
    _expected_daily,
)
from data_platform.source_spec import (
    WAREHOUSES as SPEC_WAREHOUSES,
)

CUSTOMER_NAMES = [
    "张伟", "李娜", "王芳", "刘洋", "陈静", "杨磊", "赵敏", "黄强", "周婷", "吴昊",
    "徐丽", "孙浩", "马超", "朱玲", "胡军", "郭敏", "林涛", "何静", "高翔", "罗娟",
    "郑凯", "梁艳", "谢鹏", "宋佳", "唐勇", "许梅", "韩磊", "冯雪", "曹阳", "邓丽",
]
CITIES = ["杭州市", "上海市", "北京市", "广州市", "成都市", "武汉市", "南京市", "苏州市"]
WAREHOUSES = ["WH-HZ", "WH-GZ", "WH-BJ"]
assert tuple(WAREHOUSES) == tuple(SPEC_WAREHOUSES), "仓库清单与 source_spec 漂移"


def _read_agent_db(path: Path) -> dict:
    """从 Agent 演示库读取 ERP 目录/在途/销量流水（只读；需显式配置 PLATFORM_SOURCE_DB）。"""
    if not path.exists():
        raise FileNotFoundError(f"未找到 {path}，请先运行 scripts/init_db.py 生成演示数据")
    con = sqlite3.connect(path)
    try:
        con.row_factory = sqlite3.Row
        skus = [dict(r) for r in con.execute(
            "select sku_id, style_id, style_name, category, season, color, size, warehouse_id, "
            "platform, price, stock, return_rate, supplier_id from sku"
        )]
        suppliers = {r["id"]: dict(r) for r in con.execute(
            "select id, name, lead_time_days, moq from supplier"
        )}
        in_transit = [dict(r) for r in con.execute(
            "select sku_id, qty, eta, po_no, status from in_transit"
        )]
        today = dt.date.today()
        rows = con.execute(
            "select sku_id, date(stat_hour) as d, sum(qty) as q from stat_hour "
            "where date(stat_hour) >= date(?, '-30 day') group by sku_id, d",
            (today.isoformat(),),
        )
        daily_sales = [dict(r) for r in rows]
    finally:
        con.close()
    return {"skus": skus, "suppliers": suppliers, "in_transit": in_transit,
            "daily_sales": daily_sales, "source": f"agent-db:{path}"}


def _load_source(today: dt.date, seed: int) -> dict:
    """默认自举（中台自带目录）；只有显式配置且文件存在时才复用 Agent 演示库。"""
    if SOURCE_DB and SOURCE_DB.exists():
        return _read_agent_db(SOURCE_DB)
    from data_platform.source_spec import build_source

    return build_source(today, seed)


def _write_json(name: str, payload: dict) -> Path:
    path = RAW_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)      # 原子替换：重建快照时读者不会读到半截文件
    return path


def generate_all(snapshot_date: dt.date | None = None, seed: int = 42) -> dict:
    today = snapshot_date or dt.date.today()
    rng = random.Random(seed)
    src = _load_source(today, seed)
    skus = src["skus"]
    suppliers = src["suppliers"]
    by_style = {spec["style_id"]: spec for spec in STYLES}

    # 1) ERP 库存快照（主仓 + 部分 SKU 的第二仓库存，用于调拨建议）
    lead_by_supplier = {sid: s.get("lead_time_days", 10) for sid, s in suppliers.items()}
    inventory: list[dict] = []
    for s in skus:
        stock = int(s.get("stock") or 0)
        lead = lead_by_supplier.get(s.get("supplier_id"), 10)
        inventory.append({
            "sku_id": s["sku_id"], "style_id": s["style_id"], "style_name": s["style_name"],
            "category": s["category"], "season": s["season"], "color": s["color"], "size": s["size"],
            "warehouse_id": s["warehouse_id"], "price": float(s["price"] or 0),
            "list_price": float(s.get("list_price") or s.get("price") or 0),
            "launch_days_ago": int(s.get("launch_days_ago") or 120),
            "on_hand": stock, "locked": int(stock * 0.05),
            "return_rate": float(s["return_rate"] or 0),
            "supplier_id": s["supplier_id"], "lead_time_days": lead,
        })
        if stock > 20 and rng.random() < 0.35:
            other = rng.choice([w for w in WAREHOUSES if w != s["warehouse_id"]])
            inventory.append({
                "sku_id": s["sku_id"], "style_id": s["style_id"], "style_name": s["style_name"],
                "category": s["category"], "season": s["season"], "color": s["color"], "size": s["size"],
                "warehouse_id": other, "price": float(s["price"] or 0),
                "list_price": float(s.get("list_price") or s.get("price") or 0),
                "launch_days_ago": int(s.get("launch_days_ago") or 120),
                "on_hand": int(stock * rng.uniform(0.1, 0.3)), "locked": 0,
                "return_rate": float(s["return_rate"] or 0),
                "supplier_id": s["supplier_id"], "lead_time_days": lead,
            })

    # 2) ERP 在途采购（沿用演示库，若为空则补充合成）
    in_transit: list[dict] = []
    supplier_by_sku = {s["sku_id"]: s.get("supplier_id", "") for s in skus}
    for it in src["in_transit"]:
        eta = it.get("eta") or (today + dt.timedelta(days=7)).isoformat()
        in_transit.append({
            "po_no": it.get("po_no") or f"PO{it['sku_id'][-3:]}",
            "sku_id": it["sku_id"], "qty": int(it.get("qty") or 0),
            "eta": str(eta), "supplier_id": supplier_by_sku.get(it["sku_id"], "SUP-01"), "status": "在途",
        })

    # 3) CRM 客户
    customers: list[dict] = []
    for i, name in enumerate(CUSTOMER_NAMES):
        customers.append({
            "customer_id": f"C{i + 1:03d}",
            "name": name,
            "phone": f"138{rng.randint(1000, 9999)}{rng.randint(1000, 9999)}",
            "tier": "VIP" if i % 5 == 0 else "普通",
            "region": rng.choice(CITIES),
        })

    # 4) 订单库（按 SKU 日销量生成订单明细，近 2 天为待发货）
    sales_by_sku_day: dict[tuple[str, str], int] = {}
    for r in src["daily_sales"]:
        sales_by_sku_day[(r["sku_id"], str(r["d"]))] = int(r["q"] or 0)
    price_by_sku = {s["sku_id"]: float(s["price"] or 0) for s in skus}
    orders: list[dict] = []
    order_seq = 0
    for (sku_id, day), qty in sorted(sales_by_sku_day.items()):
        if qty <= 0:
            continue
        order_seq += 1
        day_date = dt.date.fromisoformat(day)
        pending = (today - day_date).days <= 2
        cust = rng.choice(customers)
        orders.append({
            "order_id": f"ORD{order_seq:06d}",
            "sku_id": sku_id,
            "customer_id": cust["customer_id"],
            "qty": qty,
            "amount": round(qty * price_by_sku.get(sku_id, 0) * rng.uniform(0.85, 0.95), 2),
            "order_date": day,
            "status": "待发货" if pending else "已发货",
            "receiver": f"{cust['name']}（{cust['region']}）",
        })

    # 5) CRM 预订：对「库存偏紧」的 SKU 生成客户预订（用于缺货影响评估）
    stock_by_sku = {s["sku_id"]: int(s.get("stock") or 0) for s in skus}
    qty7: dict[str, int] = {}
    for (sku_id, _day), qty in sales_by_sku_day.items():
        qty7[sku_id] = qty7.get(sku_id, 0) + qty
    tight = [
        sku_id for sku_id in stock_by_sku
        if sku_id in qty7 and qty7[sku_id] > 0
        and stock_by_sku[sku_id] / max(qty7[sku_id] / 7, 0.5) < 20
    ]
    rng.shuffle(tight)
    reservations: list[dict] = []
    for sku_id in tight[:25]:
        for _ in range(rng.randint(1, 2)):
            cust = rng.choice(customers)
            reservations.append({
                "reservation_id": f"RSV{rng.randint(100000, 999999)}",
                "sku_id": sku_id,
                "customer_id": cust["customer_id"],
                "qty": rng.randint(5, 60),
                "reserved_at": (today - dt.timedelta(days=rng.randint(0, 3))).isoformat() + " 10:00:00",
                "expected_ship": (today + dt.timedelta(days=rng.randint(1, 7))).isoformat(),
                "status": "待发货",
            })

    suppliers_out = [
        {"supplier_id": sid, "name": s.get("name", sid), "lead_time_days": s.get("lead_time_days", 10),
         "moq": s.get("moq", 100), "on_time_rate": s.get("on_time_rate", 0.9)}
        for sid, s in suppliers.items()
    ]

    # 6) 物流单：已发货/待发货订单各生成一张物流单（服装退货多半死在物流体验上）
    logistics: list[dict] = []
    for o in orders:
        shipped = o["status"] == "已发货"
        eta_days = rng.randint(-2, 3) if shipped else rng.randint(1, 5)
        logistics.append({
            "order_id": o["order_id"],
            "carrier": rng.choice(CARRIERS),
            "tracking_no": f"{rng.choice(['SF', 'ZT', 'YT', 'YD'])}{rng.randint(10**11, 10**12 - 1)}",
            "status": rng.choice(LOGISTICS_STATES) if shipped else "已揽收",
            "eta": (today + dt.timedelta(days=eta_days)).isoformat(),
            "updated_at": (today - dt.timedelta(days=rng.randint(0, 2))).isoformat() + " 18:00:00",
        })

    # 7) 售后工单：按行业真实分布（尺码不合适占近一半），只挂在已发货订单上
    reasons = list(AFTER_SALES_REASON_WEIGHT)
    weights = [AFTER_SALES_REASON_WEIGHT[r] for r in reasons]
    tickets: list[dict] = []
    ticket_seq = 0
    for o in orders:
        if o["status"] != "已发货" or rng.random() > 0.12:
            continue
        ticket_seq += 1
        qty = 1 if int(o["qty"]) <= 1 else rng.randint(1, min(2, int(o["qty"])))
        tickets.append({
            "ticket_id": f"AS{ticket_seq:06d}",
            "order_id": o["order_id"],
            "sku_id": o["sku_id"],
            "reason": rng.choices(reasons, weights=weights, k=1)[0],
            "qty": qty,
            "status": "pending_confirmation" if ticket_seq % 4 == 0 else "approved",
            "created_at": (today - dt.timedelta(days=rng.randint(0, 6))).isoformat() + " 10:30:00",
        })

    # 8) 竞品行情（同款比价）：每个款式 2~4 个竞品，价格围绕本款吊牌价浮动
    competitors: list[dict] = []
    for spec in STYLES:
        if rng.random() < 0.15:       # 少量新款还没被同行跟卖，用于演示 found=false 分支
            continue
        shops = rng.sample(list(COMPETITOR_SHOPS), k=rng.randint(2, 4))
        for shop_name, platform in shops:
            price = round(float(spec["price"]) * rng.uniform(0.72, 1.18), 2)
            sales_30d = max(5, int(round(spec["base_daily"] * rng.uniform(12, 40))))
            sales_7d = max(1, int(round(sales_30d / 30 * 7 * rng.uniform(0.6, 1.6))))
            competitors.append({
                "style_id": spec["style_id"], "shop_name": shop_name, "platform": platform,
                "price": price, "sales_30d": sales_30d, "sales_7d": sales_7d,
                "rating": round(rng.uniform(4.3, 4.9), 2),
                "trend_pct": round(sales_7d / max(sales_30d / 30 * 7, 1) - 1.0, 4),
                "snapshot_ts": today.isoformat() + " 03:00:00",
            })

    # 9) 平台大促窗口（Agent 侧「要不要提前备货/锁定库存」的依据）
    activities = [
        {
            "name": a["name"], "platform": a["platform"],
            "start_date": (today + dt.timedelta(days=a["start_offset"])).isoformat(),
            "end_date": (today + dt.timedelta(days=a["start_offset"] + a["span"])).isoformat(),
            "category": a["category"], "note": a["note"],
        }
        for a in PLATFORM_ACTIVITIES
    ]

    # 10) 仓间在途调拨：主仓缺货、次仓有货的 SKU 才会调拨
    transfers: list[dict] = []
    transfer_seq = 0
    for sku in skus:
        if int(sku.get("stock") or 0) <= 0:
            continue
        if rng.random() > 0.06:
            continue
        other = rng.choice([w for w in WAREHOUSES if w != sku["warehouse_id"]])
        transfer_seq += 1
        transfers.append({
            "transfer_no": f"TR{today:%y%m}{transfer_seq:04d}",
            "sku_id": sku["sku_id"],
            "from_warehouse_id": other, "to_warehouse_id": sku["warehouse_id"],
            "qty": max(5, int(round(_expected_daily(by_style[sku["style_id"]], sku["size"]) * 7))),
            "eta": (today + dt.timedelta(days=rng.randint(1, 4))).isoformat(),
            "status": "在途",
        })

    meta = {
        "snapshot_date": today.isoformat(),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        # 数据来源可追溯：synthetic=中台自举；agent-db:<path>=显式复用 Agent 演示库
        "source": src.get("source", "synthetic"),
        "counts": {
            "inventory": len(inventory), "in_transit": len(in_transit),
            "customers": len(customers), "orders": len(orders), "reservations": len(reservations),
            "logistics": len(logistics), "after_sales": len(tickets),
            "competitors": len(competitors), "activities": len(activities),
            "transfers": len(transfers), "suppliers": len(suppliers_out),
        },
    }

    paths = {
        "erp_inventory": _write_json("erp_inventory.json", {"meta": meta, "items": inventory}),
        "erp_in_transit": _write_json("erp_in_transit.json", {"meta": meta, "items": in_transit}),
        "erp_suppliers": _write_json("erp_suppliers.json", {"meta": meta, "items": suppliers_out}),
        "crm_customers": _write_json("crm_customers.json", {"meta": meta, "items": customers}),
        "crm_reservations": _write_json("crm_reservations.json", {"meta": meta, "items": reservations}),
        "orders": _write_json("orders.json", {"meta": meta, "items": orders}),
        "logistics": _write_json("logistics.json", {"meta": meta, "items": logistics}),
        "after_sales": _write_json("after_sales.json", {"meta": meta, "items": tickets}),
        "competitors": _write_json("competitors.json", {"meta": meta, "items": competitors}),
        "platform_activities": _write_json("platform_activities.json",
                                           {"meta": meta, "items": activities}),
        "transfers": _write_json("transfers.json", {"meta": meta, "items": transfers}),
    }
    return {"meta": meta, "paths": {k: str(v) for k, v in paths.items()}}


def load_snapshot(name: str) -> dict:
    path = RAW_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"快照不存在：{path}，请先运行 mock_sources.generate_all()")
    return json.loads(path.read_text(encoding="utf-8"))
