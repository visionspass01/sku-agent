"""本地模拟数据生成器（确定性随机，固定 seed 可复现）。

生成口径尽量贴近真实业务场景：
- 20 款 × 3 色 × 5 码 = 300 SKU；
- 4 家供应商（不同交期/MOQ）、3 个仓、2 个平台；
- 近 30 天按小时出库流水（周末系数 + 季节曲线 + 品类差异）；
- 按业务规则注入 7 类异常 SKU（断码/下滑/积压/换季/退货率/缺货/大促备货），
  并写 `eval_label` 作为评估脚本的 ground-truth。
"""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass, field

import numpy as np
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app.db.models import (
    AfterSalesTicket,
    CompetitorListing,
    EvalLabel,
    Flow,
    InTransit,
    MockLogistics,
    MockOrder,
    PlatformActivity,
    Sku,
    StatHour,
    Supplier,
    TransferOrder,
    Warehouse,
)

SAMPLE_ORDERS = 120

SIZE_WEIGHT = {
    "S": 0.7, "M": 1.0, "L": 1.15, "XL": 0.9, "XXL": 0.6,
    "90": 0.8, "100": 1.0, "110": 1.0, "120": 0.9, "130": 0.7,
}

WEEKDAY_FACTOR = np.array([0.95, 0.9, 0.92, 1.0, 1.05, 1.12, 1.06])  # 周一..周日

CATEGORY_FACTOR = {"女装": 1.2, "男装": 1.0, "童装": 0.9}
PLATFORM_FACTOR = {"淘系": 1.0, "抖音": 1.15}


@dataclass
class StyleSpec:
    style_id: str
    name: str
    category: str
    season: str
    base_daily: float
    price: float
    supplier_id: str
    warehouse_id: str
    platform: str
    anomaly: str = ""  # "" | 断码 | 下滑 | 积压 | 换季 | 退货率 | 缺货 | 大促备货
    has_competitor: bool = True
    sizes: tuple[str, ...] = ("S", "M", "L", "XL", "XXL")
    colors: tuple[str, ...] = ("黑色", "杏色", "雾霾蓝")


@dataclass
class SeedSummary:
    skus: int = 0
    flows: int = 0
    stat_rows: int = 0
    competitors: int = 0
    labels: int = 0
    anomalies_labeled: int = 0
    start: dt.datetime = field(default_factory=lambda: dt.datetime.now())
    end: dt.datetime = field(default_factory=lambda: dt.datetime.now())


def _default_styles() -> list[StyleSpec]:
    return [
        StyleSpec("ST-01", "轻暖短款羽绒服", "女装", "秋冬", 18.0, 899, "SUP-01", "WH-HZ", "淘系"),
        StyleSpec("ST-02", "高领羊绒打底衫", "女装", "秋冬", 12.0, 399, "SUP-02", "WH-HZ", "淘系", anomaly="断码"),
        StyleSpec("ST-03", "羊毛混纺阔腿裤", "女装", "四季", 14.0, 329, "SUP-03", "WH-GZ", "抖音"),
        StyleSpec("ST-04", "复古灯芯绒外套", "女装", "秋冬", 9.0, 569, "SUP-01", "WH-HZ", "淘系"),
        StyleSpec("ST-05", "加厚连帽卫衣", "男装", "秋冬", 40.0, 259, "SUP-02", "WH-HZ", "抖音", anomaly="下滑"),
        StyleSpec("ST-06", "商务休闲西裤", "男装", "四季", 11.0, 299, "SUP-03", "WH-GZ", "淘系"),
        StyleSpec("ST-07", "工装多袋夹克", "男装", "秋冬", 10.0, 459, "SUP-04", "WH-BJ", "淘系"),
        StyleSpec("ST-08", "纯棉百搭打底裤", "女装", "四季", 8.0, 129, "SUP-02", "WH-HZ", "淘系", has_competitor=False),
        StyleSpec("ST-09", "薄款长袖防晒T恤", "男装", "春夏", 1.5, 159, "SUP-04", "WH-HZ", "淘系", anomaly="积压"),
        StyleSpec("ST-10", "冰丝锥形休闲裤", "男装", "春夏", 16.0, 229, "SUP-03", "WH-GZ", "抖音"),
        StyleSpec("ST-11", "法式碎花半身裙", "女装", "春夏", 13.0, 269, "SUP-01", "WH-GZ", "淘系"),
        StyleSpec("ST-12", "山系连帽冲锋衣", "女装", "秋冬", 9.0, 759, "SUP-02", "WH-HZ", "抖音", anomaly="换季"),
        StyleSpec("ST-13", "纯棉落肩短袖", "男装", "春夏", 15.0, 139, "SUP-04", "WH-HZ", "淘系"),
        StyleSpec("ST-14", "凉感防晒皮肤衣", "男装", "春夏", 12.0, 199, "SUP-01", "WH-GZ", "淘系"),
        StyleSpec("ST-15", "儿童加绒运动裤", "童装", "秋冬", 14.0, 159, "SUP-03", "WH-BJ", "淘系", anomaly="退货率"),
        StyleSpec("ST-16", "儿童防晒连体衣", "童装", "春夏", 10.0, 189, "SUP-02", "WH-HZ", "抖音",
                  sizes=("90", "100", "110", "120", "130")),
        StyleSpec("ST-17", "婴儿纯棉家居服", "童装", "四季", 9.0, 149, "SUP-04", "WH-GZ", "淘系",
                  sizes=("90", "100", "110", "120", "130")),
        StyleSpec("ST-18", "厚底老爹鞋（联名款）", "女装", "四季", 22.0, 499, "SUP-01", "WH-HZ", "淘系", anomaly="缺货"),
        StyleSpec("ST-19", "针织开衫披肩", "女装", "秋冬", 7.0, 359, "SUP-03", "WH-BJ", "抖音", has_competitor=False),
        StyleSpec("ST-20", "羽绒马甲（大促主推）", "女装", "秋冬", 30.0, 599, "SUP-02", "WH-HZ", "淘系", anomaly="大促备货"),
    ]


def seed_database(
    session: Session,
    now: dt.datetime | None = None,
    n_days: int = 30,
    seed: int = 42,
) -> SeedSummary:
    """写入全部业务模拟数据；`now` 决定数据截止时刻（默认当前整点）。"""
    now = (now or dt.datetime.now()).replace(minute=0, second=0, microsecond=0)
    start = now - dt.timedelta(days=n_days)
    rng = np.random.default_rng(seed)

    _seed_warehouses(session)
    _seed_suppliers(session)
    _seed_activities(session, now)
    # 维度表先落库：MySQL 强制外键，后面的 sku 依赖 warehouse / supplier
    session.flush()

    styles = _default_styles()
    summary = SeedSummary(start=start, end=now)
    labels: list[EvalLabel] = []
    flow_rows: list[dict] = []
    in_transit_rows: list[dict] = []
    transfer_rows: list[dict] = []

    for spec in styles:
        _seed_style(session, spec, rng, now, start, labels, flow_rows,
                    in_transit_rows, transfer_rows)

    # 主表 sku 先落库，再批量写依赖它的子表（in_transit / transfer_order / eval_label）
    session.flush()
    if in_transit_rows:
        session.execute(insert(InTransit), in_transit_rows)
    if transfer_rows:
        session.execute(insert(TransferOrder), transfer_rows)

    _seed_competitors(session, styles, rng, now)
    session.add_all(labels)
    session.flush()
    summary.skus = len(labels)
    summary.competitors = session.query(CompetitorListing).count()
    summary.labels = len(labels)
    summary.anomalies_labeled = sum(1 for lb in labels if lb.anomaly_type != "normal")
    session.flush()

    # 批量写流水（SQLAlchemy Core bulk insert，演示规模下远快于逐条 ORM add）
    for i in range(0, len(flow_rows), 5000):
        session.execute(insert(Flow), flow_rows[i:i + 5000])
    session.flush()

    # 快照表由流水聚合（真实链路：定时任务读流水 → 写 stat_hour）
    from app.pipeline.snapshot import rebuild_stat_hours

    rebuild_stat_hours(session, start, now)
    summary.flows = len(flow_rows)
    summary.stat_rows = session.query(StatHour).count()

    # 数据驱动的补充标注：非在店季节的高库存（如 11 月的春夏款）语义上也是库存风险，
    # 否则评估时会把「合理的季节积压告警」误算成假阳性。
    _complete_season_labels(session, now)
    return summary


ACTIVE_MONTHS = {
    "秋冬": {9, 10, 11, 12, 1, 2},
    "春夏": {3, 4, 5, 6, 7, 8},
}


def _complete_season_labels(session: Session, now: dt.datetime) -> None:
    from sqlalchemy import func, select

    seven_days_ago = now - dt.timedelta(days=7)
    rows = session.execute(
        select(StatHour.sku_id, func.sum(StatHour.qty)).where(StatHour.stat_hour >= seven_days_ago)
        .group_by(StatHour.sku_id)
    ).all()
    sales = {sku_id: int(qty or 0) for sku_id, qty in rows}
    month = now.month
    for lb in session.scalars(select(EvalLabel).where(EvalLabel.anomaly_type == "normal")):
        sku = session.get(Sku, lb.sku_id)
        if sku is None or sku.season == "四季":
            continue
        base_daily = sales.get(lb.sku_id, 0) / 7.0
        days = float(sku.stock) / max(base_daily, 0.2)
        if month in ACTIVE_MONTHS.get(sku.season, set()) or days < 60:
            continue
        lb.anomaly_type = "offseason"
        lb.expected_risk = "中"
        lb.note = "数据驱动标注：非在店季节的高库存（可售天数 ≥ 60）"


def _seed_warehouses(session: Session) -> None:
    session.add_all(
        [
            Warehouse(id="WH-HZ", name="杭州1仓", city="杭州"),
            Warehouse(id="WH-GZ", name="广州1仓", city="广州"),
            Warehouse(id="WH-BJ", name="北京1仓", city="北京"),
        ]
    )


def _seed_suppliers(session: Session) -> None:
    session.add_all(
        [
            Supplier(id="SUP-01", name="宁波华宇服饰", lead_time_days=12, moq=200, on_time_rate=0.9),
            Supplier(id="SUP-02", name="东莞丽丰针织", lead_time_days=7, moq=300, on_time_rate=0.85),
            Supplier(id="SUP-03", name="南通锦程家纺", lead_time_days=15, moq=150, on_time_rate=0.93),
            Supplier(id="SUP-04", name="杭州衣缘供应链", lead_time_days=9, moq=100, on_time_rate=0.88),
        ]
    )


def _seed_activities(session: Session, now: dt.datetime) -> None:
    today = now.date()
    session.add_all(
        [
            PlatformActivity(
                name="平台大促预热（模拟）", platform="淘系",
                start_date=today - dt.timedelta(days=3), end_date=today + dt.timedelta(days=12),
                category="全部", note="大促报名 SKU 需报备 7 天以上可售库存，主推款不得断码",
            ),
            PlatformActivity(
                name="双11服饰会场", platform="淘系",
                start_date=dt.date(2026, 10, 24), end_date=dt.date(2026, 11, 11),
                category="女装", note="报名门槛：近 30 天动销≥80%，预售款需在 10-30 前到仓",
            ),
            PlatformActivity(
                name="618 年中大促", platform="淘系",
                start_date=dt.date(2026, 6, 1), end_date=dt.date(2026, 6, 20),
                category="全部", note="现货库存≥5天可售，活动期间发货时效 48h",
            ),
            PlatformActivity(
                name="年货节", platform="抖音",
                start_date=dt.date(2026, 1, 10), end_date=dt.date(2026, 1, 25),
                category="全部", note="直播爆款需备 10 天库存",
            ),
        ]
    )


def _season_factor(season: str, d: dt.date) -> float:
    """平滑季节系数（余弦峰谷），避免月初阶跃掩盖销量趋势。

    秋冬峰值 12 月 15 日、春夏峰值 6 月 18 日；取值区间约 [0.25, 1.10]。
    """
    if season == "四季":
        return 0.8
    doy = d.timetuple().tm_yday
    peak = 349 if season == "秋冬" else 169
    phase = 2 * 3.141592653589793 * (doy - peak) / 365
    return round(0.25 + 0.85 * (0.5 + 0.5 * math.cos(phase)), 4)


def _seed_style(
    session: Session,
    spec: StyleSpec,
    rng: np.random.Generator,
    now: dt.datetime,
    start: dt.datetime,
    labels: list[EvalLabel],
    flow_rows: list[dict],
    in_transit_rows: list[dict],
    transfer_rows: list[dict],
) -> None:
    n_hours = int((now - start).total_seconds() // 3600) + 1
    hours = np.arange(n_hours)
    dts = [start + dt.timedelta(hours=int(h)) for h in hours]
    dow_factor = np.array([WEEKDAY_FACTOR[d.weekday()] for d in dts])
    season_factor = np.array([_season_factor(spec.season, d) for d in dts])
    season_mean = float(season_factor.mean())
    cat_plat = CATEGORY_FACTOR.get(spec.category, 1.0) * PLATFORM_FACTOR.get(spec.platform, 1.0)
    lead_days = {"SUP-01": 12, "SUP-02": 7, "SUP-03": 15, "SUP-04": 9}[spec.supplier_id]

    for ci, color in enumerate(spec.colors):
        color_w = rng.uniform(0.85, 1.15)
        for size in spec.sizes:
            sku_id = f"{spec.style_id}-C{ci + 1}-{size}"
            weight = SIZE_WEIGHT.get(size, 1.0) * color_w
            planned_daily = spec.base_daily * weight
            # 按「当月季节系数」折算的预期日销：库存/可售天数口径与销量口径一致，
            # 否则淡季销量低会把所有 SKU 误判为积压。
            expected_daily = planned_daily * cat_plat * season_mean
            anomaly = spec.anomaly if _anomaly_applies(spec.anomaly, ci) else ""
            stock, return_rate = _stock_and_return(anomaly, expected_daily, size, rng)

            sku = Sku(
                sku_id=sku_id, style_id=spec.style_id, style_name=spec.name,
                category=spec.category, season=spec.season, color=color, size=size,
                warehouse_id=spec.warehouse_id, platform=spec.platform,
                price=round(spec.price * rng.uniform(0.95, 1.0), 2),
                list_price=spec.price,
                stock=int(stock), return_rate=round(float(return_rate), 4),
                supplier_id=spec.supplier_id, status="在售",
                list_date=(now - dt.timedelta(days=int(rng.integers(20, 400)))).date(),
            )
            session.add(sku)

            # ---- 小时销量（Poisson 过程 + 季节/周内 + 异常趋势） ----
            mu = (planned_daily / 24.0) * cat_plat
            if anomaly == "下滑":
                # 近 10 天按天衰减 ≈ 18%/天（近 7 天累计下降 ~65%），保证可被量化筛查识别
                decay = np.where(
                    hours >= n_hours - 10 * 24,
                    np.exp(-0.18 * (hours - (n_hours - 10 * 24)) / 24.0),
                    1.0,
                )
                mu = mu * decay
            qty = rng.poisson(mu * dow_factor * season_factor)
            amt_per = round(spec.price * rng.uniform(0.82, 0.95), 2)
            for h in np.nonzero(qty)[0]:
                flow_rows.append({
                    "sku_id": sku_id,
                    "warehouse_id": spec.warehouse_id,
                    "qty": int(qty[h]),
                    "amt": round(int(qty[h]) * amt_per, 2),
                    "ts": dts[int(h)],
                    "flow_type": "OUT",
                    "order_id": f"SO{int(h) % 9973:04d}{int(rng.integers(0, 9999)):04d}",
                })

            # ---- 在途采购：约 20% SKU 有在途，缺货款刻意交期偏长 ----
            # 注意：这里只收集行，**不立即 add**。MySQL 会强制外键，而 SQLAlchemy 对
            # 「只有 ForeignKey 列、没有 relationship」的表不保证插入顺序，直接 add 会让
            # in_transit 先于 sku 落库（本地 SQLite 默认不校验外键，所以过去没暴露）。
            if rng.random() < 0.2 or anomaly == "缺货":
                it_eta = lead_days if anomaly == "缺货" else int(rng.integers(1, lead_days + 3))
                in_transit_rows.append({
                    "sku_id": sku_id,
                    "qty": int(max(expected_daily * lead_days * rng.uniform(0.6, 1.4), 5)),
                    "eta": (now + dt.timedelta(days=it_eta)).date(),
                    "po_no": f"PO{int(rng.integers(1000, 9999))}",
                    "status": "在途",
                })

            label_type = anomaly
            if anomaly == "断码" and size not in ("S", "M"):
                label_type = ""  # 断码款里未断码的尺码只是旁证，不标注为异常
            labels.append(EvalLabel(
                sku_id=sku_id,
                anomaly_type=label_type or "normal",
                expected_risk=_expected_risk(label_type),
                note=_anomaly_note(label_type),
            ))

    if spec.anomaly in ("断码", "缺货", "积压"):
        for _ in range(2):
            transfer_rows.append({
                "sku_id": f"{spec.style_id}-C1-{spec.sizes[1]}",
                "from_warehouse_id": "WH-GZ" if spec.warehouse_id != "WH-GZ" else "WH-HZ",
                "to_warehouse_id": spec.warehouse_id,
                "qty": int(rng.integers(20, 80)),
                "eta": (now + dt.timedelta(days=int(rng.integers(2, 8)))).date(),
                "status": "调拨中",
            })


def _anomaly_applies(anomaly: str, color_idx: int) -> bool:
    """异常按颜色子集注入，贴近「同款不同色/码表现分化」的真实情况。"""
    if not anomaly:
        return False
    if anomaly in ("断码", "下滑"):
        return True
    return {
        "积压": color_idx == 0,
        "缺货": color_idx == 0,
        "大促备货": color_idx == 0,
        "换季": color_idx == 1,
        "退货率": color_idx == 2,
    }.get(anomaly, False)


def _stock_and_return(anomaly: str, expected_daily: float, size: str, rng: np.random.Generator) -> tuple[float, float]:
    if anomaly == "断码":
        if size in ("S", "M"):
            return 0, float(rng.uniform(0.05, 0.1))
        return max(1, int(round(expected_daily * rng.uniform(18, 36)))), float(rng.uniform(0.05, 0.1))
    if anomaly == "下滑":
        return max(2, int(round(expected_daily * rng.uniform(18, 32)))), float(rng.uniform(0.06, 0.12))
    if anomaly == "积压":
        return int(expected_daily * rng.uniform(100, 160)), float(rng.uniform(0.03, 0.06))
    if anomaly == "换季":
        return int(expected_daily * rng.uniform(46, 58)), float(rng.uniform(0.04, 0.09))
    if anomaly == "退货率":
        return int(expected_daily * rng.uniform(14, 26)), float(rng.uniform(0.22, 0.32))
    if anomaly == "缺货":
        return max(1, int(round(expected_daily * rng.uniform(2, 4)))), float(rng.uniform(0.05, 0.1))
    if anomaly == "大促备货":
        return int(expected_daily * rng.uniform(90, 110)), float(rng.uniform(0.04, 0.08))
    # 正常：可售天数 12~40 天
    days = rng.uniform(12, 40)
    return max(1, int(round(expected_daily * days))), float(np.clip(rng.normal(0.07, 0.025), 0.02, 0.15))


def _expected_risk(anomaly: str) -> str:
    return {
        "断码": "高", "下滑": "高", "缺货": "高", "过季积压": "中",
        "换季": "中", "退货率": "中", "积压": "中", "大促备货": "低",
    }.get(anomaly, "低")


def _anomaly_note(anomaly: str) -> str:
    return {
        "断码": "模拟注入：多尺码断货，同款动销正常",
        "下滑": "模拟注入：近 7 天销量线性下滑",
        "积压": "模拟注入：低动销高库存，可售天数畸高",
        "换季": "模拟注入：季末高库存且需求不增",
        "退货率": "模拟注入：退货率显著高于品类均值",
        "缺货": "模拟注入：可售天数不足且在途交期偏长",
        "大促备货": "模拟注入：大促主推款高库存属正常备货（筛查假阳）",
    }.get(anomaly, "")


def _seed_competitors(session: Session, styles: list[StyleSpec], rng: np.random.Generator, now: dt.datetime) -> None:
    shops = ["旗舰店A", "旗舰店B", "买手店C"]
    for spec in styles:
        if not spec.has_competitor:
            continue
        for shop in shops:
            session.add(CompetitorListing(
                style_id=spec.style_id, platform=spec.platform, shop_name=shop,
                price=round(spec.price * rng.uniform(0.85, 1.06), 2),
                sales_30d=int(max(10, spec.base_daily * 30 * rng.uniform(0.7, 1.4))),
                sales_7d=int(max(1, spec.base_daily * 7 * rng.uniform(0.7, 1.4))),
                rating=round(float(rng.uniform(4.2, 4.95)), 2),
                trend_pct=round(float(rng.normal(0.05, 0.18)), 3),
                snapshot_ts=now,
            ))


# ---------------------------------------------------------------- 订单/物流/售后（MCP mock）
def seed_orders(session, seed: int = 42, sample: int = SAMPLE_ORDERS) -> dict:
    """按 flow 里的 order_id 生成订单 + 物流；返回统计。"""
    if session.scalar(select(func.count()).select_from(MockOrder)) or 0:
        return {"skipped": True, "orders": int(session.scalar(select(func.count()).select_from(MockOrder)) or 0)}

    rng = random.Random(seed)
    # 从流水里取 (order_id, sku_id, qty, amt, ts)

    flows = list(session.execute(
        select(Flow.order_id, Flow.sku_id, Flow.qty, Flow.amt, Flow.ts)
        .where(Flow.flow_type == "OUT", Flow.order_id != "")
        .order_by(Flow.ts.desc())
        .limit(sample * 8)
    ))
    picked: dict[str, tuple] = {}
    for order_id, sku_id, qty, amt, ts in flows:
        if order_id and order_id not in picked:
            picked[order_id] = (sku_id, int(qty or 0), float(amt or 0.0), ts)
        if len(picked) >= sample:
            break

    now = dt.datetime.now()
    sku_ids = {r[0] for r in picked.values() if r[0]}
    skus = {s.sku_id: s for s in session.scalars(select(Sku).where(Sku.sku_id.in_(sku_ids)))}
    statuses = ["delivered", "shipped", "paid", "delivered", "delivered", "after_sales"]
    n_logistics = 0
    for i, (order_id, (sku_id, qty, amt, ts)) in enumerate(picked.items()):
        sku = skus.get(sku_id)
        created = ts if isinstance(ts, dt.datetime) else now - dt.timedelta(days=rng.randint(1, 40))
        status = statuses[i % len(statuses)]
        paid_at = created + dt.timedelta(minutes=rng.randint(2, 30))
        shipped_at = None
        if status in ("shipped", "delivered", "after_sales"):
            shipped_at = paid_at + dt.timedelta(hours=rng.randint(6, 30))
        session.add(MockOrder(
            order_id=order_id,
            sku_id=sku_id,
            qty=max(1, qty),
            amount=amt,
            status=status,
            channel=(sku.platform if sku else "淘系") or "淘系",
            created_at=created,
            paid_at=paid_at,
            shipped_at=shipped_at,
        ))
        if shipped_at and status in ("shipped", "delivered", "after_sales"):
            carrier = ["顺丰", "中通", "圆通", "韵达"][i % 4]
            logistics_status = "已签收" if status in ("delivered", "after_sales") else "运输中"
            session.add(MockLogistics(
                order_id=order_id,
                carrier=carrier,
                tracking_no=f"{carrier[:1]}D{rng.randint(10**11, 10**12 - 1)}",
                status=logistics_status,
                eta=(shipped_at + dt.timedelta(days=rng.randint(2, 5))).date(),
                updated_at=min(now, shipped_at + dt.timedelta(hours=6)),
            ))
            n_logistics += 1
        if status == "after_sales" and rng.random() < 0.6:
            import hashlib
            import uuid

            reason = rng.choice(["尺码不合适", "质量问题", "7天无理由"])
            tqty = max(1, qty - 1)
            key = hashlib.sha1(f"{order_id}|{reason}|{tqty}".encode()).hexdigest()
            session.add(AfterSalesTicket(
                ticket_id=str(uuid.uuid4()),
                order_id=order_id,
                sku_id=sku_id,
                reason=reason,
                qty=tqty,
                status="created",
                idempotency_key=key,
                created_at=created + dt.timedelta(days=2),
            ))
    session.flush()
    n_orders = int(session.scalar(select(func.count()).select_from(MockOrder)) or 0)
    n_tickets = int(session.scalar(select(func.count()).select_from(AfterSalesTicket)) or 0)
    return {"orders": n_orders, "logistics": n_logistics, "after_sales": n_tickets}
