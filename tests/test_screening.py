"""量化筛选与规则判定测试。"""

from __future__ import annotations

import datetime as dt

from app.config import ScreeningThresholds
from app.pipeline.screening import SkuFeatures, classify, compute_features


def _features(**overrides) -> SkuFeatures:
    base = dict(
        sku_id="SKU-X", style_id="ST-X", category="女装", season="秋冬",
        stock=100, return_rate=0.05, base_daily=10.0, slope_norm=0.0, r2=0.0,
        recent_ratio=0.0, days_of_supply=10.0, zero_sizes=0,
        style_base_daily=10.0, in_transit_cover_7d=0,
    )
    base.update(overrides)
    return SkuFeatures(**base)


class TestClassify:
    def test_trend_down_rule(self):
        thr = ScreeningThresholds()
        f = _features(slope_norm=-0.6, r2=0.8, recent_ratio=-0.35)
        results = classify({f.sku_id: f}, dt.datetime(2026, 11, 2), thr)
        codes = [r["code"] for r in results[0].reasons]
        assert "trend_down" in codes
        assert results[0].severity == "高"

    def test_trend_down_requires_recent_confirmation(self):
        thr = ScreeningThresholds()
        f = _features(slope_norm=-0.6, r2=0.8, recent_ratio=-0.05)  # 近期未走弱
        results = classify({f.sku_id: f}, dt.datetime(2026, 11, 2), thr)
        assert results == []

    def test_stock_out_with_in_transit_cover(self):
        thr = ScreeningThresholds()
        f = _features(stock=5, base_daily=10, days_of_supply=0.5, in_transit_cover_7d=0)
        results = classify({f.sku_id: f}, dt.datetime(2026, 11, 2), thr)
        assert "stock_out" in [r["code"] for r in results[0].reasons]
        # 在途 7 天内可覆盖 -> 不应告警
        f2 = _features(stock=5, base_daily=10, days_of_supply=0.5, in_transit_cover_7d=70)
        assert classify({f2.sku_id: f2}, dt.datetime(2026, 11, 2), thr) == []

    def test_overstock_severe(self):
        thr = ScreeningThresholds()
        f = _features(days_of_supply=120, base_daily=0.4, slope_norm=-0.1)
        results = classify({f.sku_id: f}, dt.datetime(2026, 11, 2), thr)
        assert "overstock" in [r["code"] for r in results[0].reasons]

    def test_healthy_sku_has_no_alerts(self):
        thr = ScreeningThresholds()
        f = _features()
        assert classify({f.sku_id: f}, dt.datetime(2026, 11, 2), thr) == []


class TestFeaturesFromDb:
    def test_compute_features(self, seeded):
        from app.db.engine import session_scope

        with session_scope() as session:
            feats = compute_features(session)
        assert "ST-90-C1-M" in feats
        normal = feats["ST-90-C1-M"]
        assert normal.base_daily > 3
        assert normal.zero_sizes == 2  # S/L 断码
        over = feats["ST-91-C1-M"]
        assert over.days_of_supply > 90
