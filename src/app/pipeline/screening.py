"""多维度量化筛选：从小时快照/流水里找「值得让 Agent 深度研判」的异常 SKU。

对应简历：业务脚本对 SKU 出库流水做多维度量化筛选（归一化线性回归、销量基数、
退货率等）列入异常 SKU 列表。这里只做规则与统计，不调用 LLM——LLM 只处理筛出来的
异常 SKU，控制成本与延迟。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
from sqlalchemy.orm import Session

from app.config import ScreeningThresholds, get_settings
from app.db import repos
from app.db.models import Sku

LEVEL_HIGH = "高"
LEVEL_MID = "中"
LEVEL_LOW = "低"

# 在店季节（判定「换季风险」用）
ACTIVE_SEASON_MONTHS = {
    "秋冬": {9, 10, 11, 12, 1, 2},
    "春夏": {3, 4, 5, 6, 7, 8},
    "四季": set(range(1, 13)),
}


@dataclass
class SkuFeatures:
    sku_id: str
    style_id: str
    category: str
    season: str
    stock: int
    return_rate: float
    base_daily: float
    slope_norm: float
    r2: float
    recent_ratio: float
    days_of_supply: float
    zero_sizes: int
    style_base_daily: float
    in_transit_cover_7d: int

    def to_metrics(self) -> dict:
        return {
            "stock": self.stock,
            "return_rate": round(self.return_rate, 4),
            "base_daily": round(self.base_daily, 2),
            "slope_norm": round(self.slope_norm, 4),
            "r2": round(self.r2, 4),
            "recent_ratio": round(self.recent_ratio, 4),
            "days_of_supply": round(self.days_of_supply, 1),
            "zero_sizes": self.zero_sizes,
            "in_transit_cover_7d": self.in_transit_cover_7d,
        }


@dataclass
class ScreeningResult:
    sku_id: str
    severity: str
    reasons: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_anomaly(self) -> dict:
        return {
            "sku_id": self.sku_id,
            "severity": self.severity,
            "reasons": self.reasons,
            "metrics": self.metrics,
        }


def _hour_grid(end: dt.datetime, hours: int) -> list[dt.datetime]:
    end = end.replace(minute=0, second=0, microsecond=0)
    return [end - dt.timedelta(hours=i) for i in range(hours - 1, -1, -1)]


def compute_features(
    session: Session,
    as_of: dt.datetime | None = None,
    window_hours: int | None = None,
    thresholds: ScreeningThresholds | None = None,
) -> dict[str, SkuFeatures]:
    thresholds = thresholds or get_settings().screening
    window_hours = window_hours or thresholds.window_hours
    as_of = as_of or repos.latest_stat_hour(session)
    if as_of is None:
        return {}

    all_skus: list[Sku] = list(session.query(Sku).all())
    sku_ids = [s.sku_id for s in all_skus]
    start = as_of - dt.timedelta(hours=window_hours - 1)
    series_map = repos.get_stat_range(session, sku_ids, start, as_of)
    hours = _hour_grid(as_of, window_hours)

    # 在途 7 天内可到货的覆盖量
    from sqlalchemy import select

    from app.db.models import InTransit

    cover_rows = session.execute(
        select(InTransit.sku_id, InTransit.qty).where(InTransit.eta <= as_of.date() + dt.timedelta(days=7))
    ).all()
    cover: dict[str, int] = {}
    for sku_id, qty in cover_rows:
        cover[sku_id] = cover.get(sku_id, 0) + int(qty)

    # 同款各尺码库存（判定断码）
    style_sizes: dict[str, list[Sku]] = {}
    for s in all_skus:
        style_sizes.setdefault(s.style_id, []).append(s)

    features: dict[str, SkuFeatures] = {}
    base_daily_map: dict[str, float] = {}

    for sku in all_skus:
        pairs = series_map.get(sku.sku_id, [])
        qty_by_hour = dict(pairs)
        y = np.array([qty_by_hour.get(h, 0) for h in hours], dtype=float)

        # 小时粒度销量稀疏（Poisson 噪声大），趋势判定按「日聚合」做线性回归，
        # 日均销量基数仍按小时序列汇总，两者口径一致。
        day_count = len(y) // 24
        if day_count >= 4 and float(y.mean()) > 0:
            y_daily = y[-day_count * 24:].reshape(day_count, 24).sum(axis=1)
            t_d = np.linspace(0.0, 1.0, len(y_daily))
            slope, intercept = np.polyfit(t_d, y_daily, 1)
            slope_norm = float(slope / max(float(y_daily.mean()), 1e-6))
            pred = slope * t_d + intercept
            ss_res = float(((y_daily - pred) ** 2).sum())
            ss_tot = float(((y_daily - y_daily.mean()) ** 2).sum())
            r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
            # 近期动销比：后 3 天 vs 前 4 天（辅助判定「真下滑」而非周内波动）
            recent_ratio = float(y_daily[-3:].mean() / max(y_daily[:4].mean(), 1e-6)) - 1.0
        else:
            slope_norm, r2 = 0.0, 0.0
            recent_ratio = 0.0

        # 销量基数：近 7 天日均（若窗口不足 7 天按实际窗口折算）
        recent_hours = min(window_hours, 7 * 24, len(y))
        base_daily = float(y[-recent_hours:].sum()) / max(1.0, recent_hours / 24.0)
        base_daily_map[sku.sku_id] = base_daily

        days_of_supply = float(sku.stock) / max(base_daily, 0.2)
        zero_sizes = sum(1 for s in style_sizes[sku.style_id] if s.stock <= 0)

        features[sku.sku_id] = SkuFeatures(
            sku_id=sku.sku_id,
            style_id=sku.style_id,
            category=sku.category,
            season=sku.season,
            stock=int(sku.stock),
            return_rate=float(sku.return_rate),
            base_daily=base_daily,
            slope_norm=slope_norm,
            r2=r2,
            recent_ratio=recent_ratio,
            days_of_supply=days_of_supply,
            zero_sizes=zero_sizes,
            style_base_daily=0.0,
            in_transit_cover_7d=cover.get(sku.sku_id, 0),
        )

    for style_id, skus_list in style_sizes.items():
        total = sum(base_daily_map.get(s.sku_id, 0.0) for s in skus_list)
        for s in skus_list:
            features[s.sku_id].style_base_daily = total

    # 断码判定依赖同款动销，第二遍回填
    return features


def _monthly_active(season: str, month: int) -> bool:
    return month in ACTIVE_SEASON_MONTHS.get(season, ACTIVE_SEASON_MONTHS["四季"])


def classify(features: dict[str, SkuFeatures], as_of: dt.datetime, thresholds: ScreeningThresholds | None = None) -> list[ScreeningResult]:
    thresholds = thresholds or get_settings().screening
    results: list[ScreeningResult] = []
    month = as_of.month

    for sku_id, f in features.items():
        reasons: list[dict] = []
        levels: list[str] = []

        # 1) 下滑风险：近 7 天销量线性下滑且拟合度高，且近期动销确认走弱
        if (
            f.base_daily >= thresholds.min_daily_base
            and f.slope_norm <= thresholds.trend_slope_norm
            and f.r2 >= thresholds.trend_r2_min
            and f.recent_ratio <= thresholds.trend_recent_ratio
            and f.stock > 0
        ):
            reasons.append({
                "code": "trend_down", "level": LEVEL_HIGH,
                "message": f"近 7 天销量下滑 {abs(f.slope_norm) * 100:.0f}%（R²={f.r2:.2f}，"
                           f"近 3 天较前 4 天 {f.recent_ratio * 100:+.0f}%）",
                "metric": {"slope_norm": round(f.slope_norm, 4), "r2": round(f.r2, 4),
                           "recent_ratio": round(f.recent_ratio, 4)},
            })
            levels.append(LEVEL_HIGH)

        # 2) 缺货风险：可售天数不足（在途 7 天内到货量可部分覆盖）
        effective_stock = f.stock + f.in_transit_cover_7d
        effective_days = effective_stock / max(f.base_daily, 0.5)
        if f.base_daily >= thresholds.min_daily_base and effective_days <= thresholds.days_of_supply_low:
            reasons.append({
                "code": "stock_out", "level": LEVEL_HIGH,
                "message": (
                    f"可售天数仅 {effective_days:.1f} 天（含 7 天内在途 {f.in_transit_cover_7d} 件），"
                    f"低于 {thresholds.days_of_supply_low} 天安全线"
                ),
                "metric": {"days_of_supply": round(effective_days, 1)},
            })
            levels.append(LEVEL_HIGH)

        # 3) 断码：同款多尺码断货且同款仍在动销
        if (
            f.zero_sizes >= thresholds.zero_size_min
            and f.style_base_daily >= thresholds.min_daily_base
            and f.stock <= 0
        ):
            reasons.append({
                "code": "size_gap", "level": LEVEL_HIGH,
                "message": f"同款 {f.zero_sizes} 个尺码断货，本码已零库存但同款日均销量 {f.style_base_daily:.1f} 件",
                "metric": {"zero_sizes": f.zero_sizes, "style_base_daily": round(f.style_base_daily, 1)},
            })
            levels.append(LEVEL_HIGH)

        # 4) 积压：可售天数畸高（严重积压不依赖趋势，避免「无销量即无趋势」漏判）
        if f.days_of_supply >= thresholds.days_of_supply_severe or (
            f.days_of_supply >= thresholds.days_of_supply_blocked
            and (f.base_daily < thresholds.min_daily_base or f.slope_norm <= 0.02)
        ):
            reasons.append({
                "code": "overstock", "level": LEVEL_MID,
                "message": f"可售天数 {f.days_of_supply:.0f} 天，动销 {f.base_daily:.1f} 件/天且无增长",
                "metric": {"days_of_supply": round(f.days_of_supply, 1), "base_daily": round(f.base_daily, 2)},
            })
            levels.append(LEVEL_MID)

        # 5) 换季：在店季节季末高库存且需求不增
        if (
            _monthly_active(f.season, month)
            and 42.0 <= f.days_of_supply < thresholds.days_of_supply_blocked
            and f.slope_norm <= 0.12
        ):
            reasons.append({
                "code": "season_end", "level": LEVEL_MID,
                "message": f"{f.season}在售期临近尾声，可售 {f.days_of_supply:.0f} 天且销量未增长",
                "metric": {"days_of_supply": round(f.days_of_supply, 1)},
            })
            levels.append(LEVEL_MID)

        # 6) 退货率异常
        if f.return_rate >= thresholds.return_rate_high and f.base_daily >= thresholds.min_daily_base:
            reasons.append({
                "code": "return_rate", "level": LEVEL_MID,
                "message": f"退货率 {f.return_rate * 100:.0f}% 超品类阈值 {thresholds.return_rate_high * 100:.0f}%",
                "metric": {"return_rate": round(f.return_rate, 4)},
            })
            levels.append(LEVEL_MID)

        if reasons:
            severity = LEVEL_HIGH if LEVEL_HIGH in levels else LEVEL_MID
            results.append(ScreeningResult(
                sku_id=sku_id, severity=severity, reasons=reasons, metrics=f.to_metrics(),
            ))
    return results


def screen(session: Session, as_of: dt.datetime | None = None) -> list[ScreeningResult]:
    """执行筛选并落库 anomaly 表。"""
    as_of = as_of or repos.latest_stat_hour(session)
    if as_of is None:
        return []
    features = compute_features(session, as_of=as_of)
    results = classify(features, as_of)
    repos.replace_anomalies(session, as_of, [r.to_anomaly() for r in results])
    return results
