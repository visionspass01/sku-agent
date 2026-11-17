"""中台自有的演示数据源规格（自举用，**不读 Agent 库**）。

为什么需要这个模块（评审缺陷 2.2-7 + 部署缺陷）：
`mock_sources` 原先靠 `sqlite3` 读 `data/sku_demo.db`（Agent 侧演示库）来生成 ERP/CRM 数据，
于是启动顺序变成「先有 Agent 库，中台才能自举」：
- 平台容器没有挂 `/app/data`（`docker/docker-compose.yml`）→ `generate_all()` 抛错 →
  `ensure_init --platform && uvicorn` 直接失败 → 整个 compose 栈起不来；
- 换成 MySQL 的部署里根本没有 `sku_demo.db` 文件 → 中台 mart 空表、所有取数接口 404。

现在中台自带一份**目录规格**（款式/品类/价格/供应商/仓库/尺码/颜色），按固定算法合成
库存、在途、销量、客户与预订。运行时对 Agent 零依赖；`PLATFORM_SOURCE_DB` 显式指定时
才复用 Agent 演示库（本地便捷路径）。

与 Agent 侧的关系：**目录（id/品类/价格/供应商/仓库/尺码/颜色）必须一致**，用
`tests/test_demo_catalog_contract.py` 把两侧的规格逐项比对；库存与销量属于「各系统各自的
事实快照」，允许不同（生产中本来就是 ERP 快照 vs 治理后 mart 的差别）。
"""

from __future__ import annotations

import datetime as dt
import random

# ---------------------------------------------------------------- 目录规格（与 Agent 演示一致）
SIZES_APPAREL = ("S", "M", "L", "XL", "XXL")
SIZES_KIDS = ("90", "100", "110", "120", "130")
COLORS = ("黑色", "杏色", "雾霾蓝")

# 尺码权重：与 Agent 侧口径一致（M/L 最好卖，S/XXL 偏慢）
SIZE_WEIGHT = {
    "S": 0.7, "M": 1.0, "L": 1.15, "XL": 0.9, "XXL": 0.6,
    "90": 0.8, "100": 1.0, "110": 1.0, "120": 0.9, "130": 0.7,
}
CATEGORY_FACTOR = {"女装": 1.2, "男装": 1.0, "童装": 0.9}
PLATFORM_FACTOR = {"淘系": 1.0, "抖音": 1.15}

SUPPLIERS = {
    "SUP-01": {"name": "宁波华宇服饰", "lead_time_days": 12, "moq": 200, "on_time_rate": 0.92},
    "SUP-02": {"name": "东莞丽丰针织", "lead_time_days": 7, "moq": 300, "on_time_rate": 0.85},
    "SUP-03": {"name": "南通锦程家纺", "lead_time_days": 15, "moq": 150, "on_time_rate": 0.78},
    "SUP-04": {"name": "杭州衣缘供应链", "lead_time_days": 9, "moq": 100, "on_time_rate": 0.96},
}

WAREHOUSES = ("WH-HZ", "WH-GZ", "WH-BJ")

# 上新天数（距今天数）：与 Agent 侧 `app/db/seed.py::STYLE_LAUNCH_DAYS` 必须一致，
# 由 tests/test_demo_catalog_contract.py 断言。它是「款式生命周期」的唯一输入。
STYLE_LAUNCH_DAYS: dict[str, int] = {
    "ST-01": 25, "ST-02": 120, "ST-03": 200, "ST-04": 60, "ST-05": 150,
    "ST-06": 240, "ST-07": 45, "ST-08": 300, "ST-09": 220, "ST-10": 190,
    "ST-11": 210, "ST-12": 95, "ST-13": 170, "ST-14": 160, "ST-15": 130,
    "ST-16": 200, "ST-17": 260, "ST-18": 35, "ST-19": 55, "ST-20": 10,
}

# ---------------------------------------------------------------- 订单 / 物流 / 售后 / 竞品 / 活动
CARRIERS = ("顺丰", "中通", "圆通", "韵达")
LOGISTICS_STATES = ("已揽收", "运输中", "派送中", "已签收")
AFTER_SALES_REASONS = ("尺码不合适", "质量问题", "7天无理由", "发错货", "物流破损")
AFTER_SALES_REASON_WEIGHT = {
    # 服装的核心售后原因是「尺码不合适」——它反过来说明尺码曲线/尺码建议有问题
    "尺码不合适": 0.46, "质量问题": 0.14, "7天无理由": 0.24, "发错货": 0.06, "物流破损": 0.10,
}

COMPETITOR_SHOPS = (
    ("优衣库官方旗舰店", "淘系"), ("森马服饰旗舰店", "淘系"), ("太平鸟女装", "淘系"),
    ("抖音·轻奢衣橱", "抖音"), ("抖音·潮流前线", "抖音"), ("UR 官方", "淘系"),
)

# 平台大促窗口（相对今天的偏移天数，便于确定性复现）
PLATFORM_ACTIVITIES = (
    {"name": "双11预售", "platform": "淘系", "start_offset": -3, "span": 12,
     "category": "全部", "note": "预售定金 10/20 起，尾款 11/01 起"},
    {"name": "双11正式场", "platform": "淘系", "start_offset": 5, "span": 4,
     "category": "全部", "note": "跨店满减 300-50"},
    {"name": "抖音暖冬季", "platform": "抖音", "start_offset": 2, "span": 9,
     "category": "秋冬", "note": "直播间专属价，需提前锁定库存"},
    {"name": "黑五清仓", "platform": "全部", "start_offset": 18, "span": 5,
     "category": "全部", "note": "过季款优先报名，折扣不设下限"},
    {"name": "年货节", "platform": "全部", "start_offset": 45, "span": 10,
     "category": "全部", "note": "礼盒装优先"},
)


# (style_id, name, category, season, base_daily, price, supplier_id, warehouse_id, platform, anomaly, sizes)
STYLES: list[dict] = [
    {"style_id": "ST-01", "name": "轻暖短款羽绒服", "category": "女装", "season": "秋冬",
     "base_daily": 18.0, "price": 899.0, "supplier_id": "SUP-01", "warehouse_id": "WH-HZ",
     "platform": "淘系", "anomaly": "", "sizes": SIZES_APPAREL},
    {"style_id": "ST-02", "name": "高领羊绒打底衫", "category": "女装", "season": "秋冬",
     "base_daily": 12.0, "price": 399.0, "supplier_id": "SUP-02", "warehouse_id": "WH-HZ",
     "platform": "淘系", "anomaly": "断码", "sizes": SIZES_APPAREL},
    {"style_id": "ST-03", "name": "羊毛混纺阔腿裤", "category": "女装", "season": "四季",
     "base_daily": 14.0, "price": 329.0, "supplier_id": "SUP-03", "warehouse_id": "WH-GZ",
     "platform": "抖音", "anomaly": "", "sizes": SIZES_APPAREL},
    {"style_id": "ST-04", "name": "复古灯芯绒外套", "category": "女装", "season": "秋冬",
     "base_daily": 9.0, "price": 569.0, "supplier_id": "SUP-01", "warehouse_id": "WH-HZ",
     "platform": "淘系", "anomaly": "", "sizes": SIZES_APPAREL},
    {"style_id": "ST-05", "name": "加厚连帽卫衣", "category": "男装", "season": "秋冬",
     "base_daily": 40.0, "price": 259.0, "supplier_id": "SUP-02", "warehouse_id": "WH-HZ",
     "platform": "抖音", "anomaly": "下滑", "sizes": SIZES_APPAREL},
    {"style_id": "ST-06", "name": "商务休闲西裤", "category": "男装", "season": "四季",
     "base_daily": 11.0, "price": 299.0, "supplier_id": "SUP-03", "warehouse_id": "WH-GZ",
     "platform": "淘系", "anomaly": "", "sizes": SIZES_APPAREL},
    {"style_id": "ST-07", "name": "工装多袋夹克", "category": "男装", "season": "秋冬",
     "base_daily": 10.0, "price": 459.0, "supplier_id": "SUP-04", "warehouse_id": "WH-BJ",
     "platform": "淘系", "anomaly": "", "sizes": SIZES_APPAREL},
    {"style_id": "ST-08", "name": "纯棉百搭打底裤", "category": "女装", "season": "四季",
     "base_daily": 8.0, "price": 129.0, "supplier_id": "SUP-02", "warehouse_id": "WH-HZ",
     "platform": "淘系", "anomaly": "", "sizes": SIZES_APPAREL},
    {"style_id": "ST-09", "name": "薄款长袖防晒T恤", "category": "男装", "season": "春夏",
     "base_daily": 1.5, "price": 159.0, "supplier_id": "SUP-04", "warehouse_id": "WH-HZ",
     "platform": "淘系", "anomaly": "积压", "sizes": SIZES_APPAREL},
    {"style_id": "ST-10", "name": "冰丝锥形休闲裤", "category": "男装", "season": "春夏",
     "base_daily": 16.0, "price": 229.0, "supplier_id": "SUP-03", "warehouse_id": "WH-GZ",
     "platform": "抖音", "anomaly": "", "sizes": SIZES_APPAREL},
    {"style_id": "ST-11", "name": "法式碎花半身裙", "category": "女装", "season": "春夏",
     "base_daily": 13.0, "price": 269.0, "supplier_id": "SUP-01", "warehouse_id": "WH-GZ",
     "platform": "淘系", "anomaly": "", "sizes": SIZES_APPAREL},
    {"style_id": "ST-12", "name": "山系连帽冲锋衣", "category": "女装", "season": "秋冬",
     "base_daily": 9.0, "price": 759.0, "supplier_id": "SUP-02", "warehouse_id": "WH-HZ",
     "platform": "抖音", "anomaly": "换季", "sizes": SIZES_APPAREL},
    {"style_id": "ST-13", "name": "纯棉落肩短袖", "category": "男装", "season": "春夏",
     "base_daily": 15.0, "price": 139.0, "supplier_id": "SUP-04", "warehouse_id": "WH-HZ",
     "platform": "淘系", "anomaly": "", "sizes": SIZES_APPAREL},
    {"style_id": "ST-14", "name": "凉感防晒皮肤衣", "category": "男装", "season": "春夏",
     "base_daily": 12.0, "price": 199.0, "supplier_id": "SUP-01", "warehouse_id": "WH-GZ",
     "platform": "淘系", "anomaly": "", "sizes": SIZES_APPAREL},
    {"style_id": "ST-15", "name": "儿童加绒运动裤", "category": "童装", "season": "秋冬",
     "base_daily": 14.0, "price": 159.0, "supplier_id": "SUP-03", "warehouse_id": "WH-BJ",
     "platform": "淘系", "anomaly": "退货率", "sizes": SIZES_APPAREL},
    {"style_id": "ST-16", "name": "儿童防晒连体衣", "category": "童装", "season": "春夏",
     "base_daily": 10.0, "price": 189.0, "supplier_id": "SUP-02", "warehouse_id": "WH-HZ",
     "platform": "抖音", "anomaly": "", "sizes": SIZES_KIDS},
    {"style_id": "ST-17", "name": "婴儿纯棉家居服", "category": "童装", "season": "四季",
     "base_daily": 9.0, "price": 149.0, "supplier_id": "SUP-04", "warehouse_id": "WH-GZ",
     "platform": "淘系", "anomaly": "", "sizes": SIZES_KIDS},
    {"style_id": "ST-18", "name": "厚底老爹鞋（联名款）", "category": "女装", "season": "四季",
     "base_daily": 22.0, "price": 499.0, "supplier_id": "SUP-01", "warehouse_id": "WH-HZ",
     "platform": "淘系", "anomaly": "缺货", "sizes": SIZES_APPAREL},
    {"style_id": "ST-19", "name": "针织开衫披肩", "category": "女装", "season": "秋冬",
     "base_daily": 7.0, "price": 359.0, "supplier_id": "SUP-03", "warehouse_id": "WH-BJ",
     "platform": "抖音", "anomaly": "", "sizes": SIZES_APPAREL},
    {"style_id": "ST-20", "name": "羽绒马甲（大促主推）", "category": "女装", "season": "秋冬",
     "base_daily": 30.0, "price": 599.0, "supplier_id": "SUP-02", "warehouse_id": "WH-HZ",
     "platform": "淘系", "anomaly": "大促备货", "sizes": SIZES_APPAREL},
]

CUSTOMER_NAMES = [
    "张伟", "李娜", "王芳", "刘洋", "陈静", "杨磊", "赵敏", "黄强", "周婷", "吴昊",
    "徐丽", "孙浩", "马超", "朱玲", "胡军", "郭敏", "林涛", "何静", "高翔", "罗娟",
    "郑凯", "梁艳", "谢鹏", "宋佳", "唐勇", "许梅", "韩磊", "冯雪", "曹阳", "邓丽",
]
CITIES = ["杭州市", "上海市", "北京市", "广州市", "成都市", "武汉市", "南京市", "苏州市"]


def catalog_skus() -> list[dict]:
    """展开成 SKU 级目录：`{style}-C{颜色序号}-{尺码}`（与 Agent 演示目录同构）。"""
    rows: list[dict] = []
    for spec in STYLES:
        for ci, color in enumerate(COLORS, start=1):
            for size in spec["sizes"]:
                rows.append({
                    "sku_id": f"{spec['style_id']}-C{ci}-{size}",
                    "style_id": spec["style_id"],
                    "style_name": spec["name"],
                    "category": spec["category"],
                    "season": spec["season"],
                    "color": color,
                    "size": size,
                    "warehouse_id": spec["warehouse_id"],
                    "platform": spec["platform"],
                    "supplier_id": spec["supplier_id"],
                })
    return rows


def _expected_daily(spec: dict, size: str) -> float:
    weight = SIZE_WEIGHT.get(size, 1.0)
    factor = CATEGORY_FACTOR.get(spec["category"], 1.0) * PLATFORM_FACTOR.get(spec["platform"], 1.0)
    return max(0.05, float(spec["base_daily"]) * weight * factor)


def _stock_for(spec: dict, size: str, rng: random.Random) -> tuple[int, float]:
    """库存与退货率：按「目标可售天数」合成，并按异常标签做定向调整。"""
    daily = _expected_daily(spec, size)
    target_days = rng.uniform(18.0, 42.0)
    anomaly = spec["anomaly"]
    if anomaly == "积压":
        target_days *= 3.0
    elif anomaly == "缺货":
        target_days *= 0.25
    elif anomaly == "大促备货":
        target_days *= 2.0
    elif anomaly == "断码" and size in ("M", "L"):
        # 断码要断在**主销码**上才有业务意义：M/L 是卖得最快的两个码，
        # 它们缺货而 S/XXL 还有货，正是服装行业说的「断码压货」形态。
        return 0, round(rng.uniform(0.05, 0.12), 4)
    stock = int(round(daily * target_days))
    return_rate = 0.18 if anomaly == "退货率" else round(rng.uniform(0.03, 0.12), 4)
    return max(0, stock), return_rate


def build_source(today: dt.date | None = None, seed: int = 42) -> dict:
    """生成与 `_read_source()` 同构的数据包。

    除原有 skus / suppliers / in_transit / daily_sales 外，还生成订单域（物流、售后）、
    竞品行情、供应商主数据、平台大促窗口、仓间调拨——这些**全部由中台自己造并持有**，
    Agent 不再保留任何业务数据副本（评审 耦合点1）。
    """
    today = today or dt.date.today()
    rng = random.Random(seed)
    by_style = {spec["style_id"]: spec for spec in STYLES}

    skus: list[dict] = []
    for row in catalog_skus():
        spec = by_style[row["style_id"]]
        stock, return_rate = _stock_for(spec, row["size"], rng)
        skus.append({
            **row,
            "price": round(float(spec["price"]) * rng.uniform(0.95, 1.0), 2),
            # 吊牌价（服装行业的下单/折扣基准）：活动价 / 折扣率都相对它计算
            "list_price": float(spec["price"]),
            "stock": stock,
            "return_rate": return_rate,
            "launch_days_ago": STYLE_LAUNCH_DAYS.get(spec["style_id"], 120),
        })

    # 在途采购：高动销款更可能有在途；eta 落在 3~14 天
    in_transit: list[dict] = []
    for sku in skus:
        spec = by_style[sku["style_id"]]
        if _expected_daily(spec, sku["size"]) < 1.0 or rng.random() > 0.35:
            continue
        lead = SUPPLIERS[spec["supplier_id"]]["lead_time_days"]
        qty = max(20, int(round(_expected_daily(spec, sku["size"]) * rng.uniform(7, 21))))
        in_transit.append({
            "sku_id": sku["sku_id"],
            "qty": qty,
            "eta": (today + dt.timedelta(days=max(2, int(lead * rng.uniform(0.4, 1.2))))).isoformat(),
            "po_no": f"PO{today:%y%m}{rng.randint(1000, 9999)}",
            "status": "在途",
        })

    # 近 30 天按 (sku, day) 聚合的销量（中台自己的快照）
    daily_sales: list[dict] = []
    for sku in skus:
        spec = by_style[sku["style_id"]]
        daily = _expected_daily(spec, sku["size"])
        if spec["anomaly"] == "下滑":
            trend = [1.35, 1.2, 1.05, 0.9, 0.75, 0.6, 0.5]
        elif spec["anomaly"] == "换季":
            trend = [0.6, 0.7, 0.85, 1.0, 1.1, 1.2, 1.3]
        else:
            trend = [1.0] * 7
        for back in range(30):
            day = today - dt.timedelta(days=back)
            factor = trend[min(6, back * 7 // 30)]
            mean = daily * factor
            qty = max(0, int(round(rng.gauss(mean, max(0.6, mean ** 0.5)))))
            if qty:
                daily_sales.append({"sku_id": sku["sku_id"], "d": day.isoformat(), "q": qty})

    return {
        "skus": skus,
        "suppliers": {sid: dict(info) for sid, info in SUPPLIERS.items()},
        "in_transit": in_transit,
        "daily_sales": daily_sales,
        "source": "synthetic",
    }
