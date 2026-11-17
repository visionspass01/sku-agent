"""Skill 子图纯计算单测（复审 A1/A2）：可售天数、断码、补货测算的口径回归。

为什么单独测：这两个 Skill 是**确定性业务计算**，但此前只有图级用例顺带跑到，
断码/缺货/换季/大促/退货率、MOQ/单次上限/suggested<=0 这些分支一条断言都没有——
所以"用现存库存算可用口径""断码不按主销码"这类错误在测试与评测里都看不见。
"""

from __future__ import annotations

import math

import pytest

from app.agents.schemas import JudgeOutput, ReplenishOutput
from app.skills.inventory_judge import build_inventory_judge_skill, compute_feature_hints
from app.skills.replenishment import build_replenishment_skill
from app.skills.stock import available_of, on_hand_of, stock_source

_JUDGE = build_inventory_judge_skill()
_REPLENISH = build_replenishment_skill()


def _sku(**over) -> dict:
    base = {"sku_id": "ST-01-C1-M", "style_id": "ST-01", "category": "男装", "season": "秋冬",
            "size": "M", "stock": 100, "available": 60, "return_rate": 0.05,
            "price": 299, "platform": "淘系", "warehouse_id": "WH-HZ"}
    base.update(over)
    return base


def _features(**over) -> dict:
    base = {"base_daily": 2.0, "slope_norm": 0.0, "days_of_supply": 30.0,
            "zero_sizes": 0, "stock_source": "available", "size_semantics": "main_size",
            "main_size_count": 2, "style_broken_size_count": 0, "style_broken_sizes": [],
            "self_broken_size": False}
    base.update(over)
    return base


def _judge(sku=None, features=None, activities=None, as_of="2026-11-16") -> dict:
    out = _JUDGE.invoke({
        "sku": sku if sku is not None else _sku(),
        "features": features if features is not None else _features(),
        "sibling_sizes": [{"size": "M", "stock": 60}, {"size": "L", "stock": 40}],
        "activities": activities or [],
        "as_of": as_of,
    })
    return out["judgment"]


def _replenish(sku=None, features=None, in_transit=None, supplier=None) -> dict:
    out = _REPLENISH.invoke({
        "sku": sku if sku is not None else _sku(),
        "features": features if features is not None else _features(),
        "in_transit": in_transit or [],
        "supplier": supplier or {"lead_time_days": 10, "moq": 100},
    })
    return out["replenish"]


# ---------------------------------------------------------------- 库存口径
def test_available_of_prefers_available_even_when_zero():
    """复审 A1 回归点：`available=0` 不能被 `or` 吞成现存库存。"""
    assert available_of({"available": 0, "stock": 100}) == 0
    assert available_of({"available": 12, "stock": 100}) == 12
    assert available_of({"stock": 100}) == 100
    assert stock_source({"available": 0, "stock": 100}) == "available"
    assert stock_source({"stock": 100}) == "on_hand"
    assert on_hand_of({"available": 0, "stock": 100}) == 100


def test_feature_hints_use_available_for_days_of_supply():
    sku = _sku(stock=100, available=40)
    hints = compute_feature_hints(sku, [{"qty": 2} for _ in range(7)])
    assert hints["base_daily"] == 2.0
    assert hints["days_of_supply"] == 20.0     # 40/2，不是 100/2
    assert hints["stock_source"] == "available"


# ---------------------------------------------------------------- 研判：断码
def test_judge_flags_broken_size_when_own_main_size_empty():
    """断码压货：本 SKU 总库存还在，但本码是主销码且可用为 0。"""
    judgment = _judge(features=_features(self_broken_size=True, style_broken_size_count=1,
                                         style_broken_sizes=["M"]))
    assert "断码" in judgment["risk_flags"]
    assert judgment["self_broken_size"] is True
    assert any("本码为主销码" in n for n in judgment["notes"])


def test_judge_does_not_flag_broken_for_long_tail_size():
    """长尾码缺货不算断码（AGENTS.md）：本码不是主销码就不打标签，只在 notes 说明同款情况。"""
    judgment = _judge(features=_features(self_broken_size=False, style_broken_size_count=2,
                                         style_broken_sizes=["S", "L"]))
    assert "断码" not in judgment["risk_flags"]
    assert any("同款主销码断货 2 个" in n for n in judgment["notes"])


def test_judge_legacy_semantics_falls_back_and_says_so():
    """旧归档没有主销码标记：回落到旧判据，并把 semantics 标成 legacy。"""
    features = _features(size_semantics="legacy", self_broken_size=None,
                         style_broken_size_count=None, zero_sizes=3)
    judgment = _judge(sku=_sku(stock=0, available=0), features=features)
    assert "断码" in judgment["risk_flags"]
    assert judgment["size_semantics"] == "legacy"
    assert any("旧口径" in n for n in judgment["notes"])


# ---------------------------------------------------------------- 研判：其它标签
def test_judge_flags_shortage_stockpile_season_promo_return():
    assert "缺货" in _judge(features=_features(days_of_supply=5, base_daily=4))["risk_flags"]
    assert "积压候选" in _judge(features=_features(days_of_supply=90, slope_norm=0.0))["risk_flags"]
    season = _judge(features=_features(days_of_supply=50, slope_norm=0.0), as_of="2026-11-16")
    assert "换季季末" in season["risk_flags"]
    promo = _judge(features=_features(days_of_supply=70, slope_norm=0.5, base_daily=5),
                   activities=[{"days_to_start": 10}])
    assert "大促备货" in promo["risk_flags"]
    assert "退货率异常" in _judge(sku=_sku(return_rate=0.2))["risk_flags"]


def test_judge_health_score_is_bounded():
    judgment = _judge(
        sku=_sku(stock=0, available=0, return_rate=0.3),
        features=_features(days_of_supply=0.5, base_daily=9, slope_norm=0.0,
                           self_broken_size=True, style_broken_size_count=3),
    )
    assert 0 <= judgment["health_score"] <= 100
    assert judgment["available_stock"] == 0


def test_judge_marks_on_hand_fallback():
    judgment = _judge(sku={"sku_id": "ST-01-C1-M", "season": "秋冬", "stock": 100},
                      features=_features(stock_source="on_hand"))
    assert judgment["stock_source"] == "on_hand"
    assert any("现存库存估算" in n for n in judgment["notes"])


# ---------------------------------------------------------------- 补货测算
def test_replenish_formulas_match_sop():
    out = _replenish(sku=_sku(stock=100, available=100),
                     features=_features(base_daily=10.0),
                     supplier={"lead_time_days": 10, "moq": 100})
    assert out["safety_stock"] == math.ceil(10 * 10 * 0.3)          # 30
    assert out["reorder_point"] == math.ceil(10 * 13) + 30          # 160
    assert out["target_days"] == 31
    assert out["suggested_qty"] == 300                              # 240 → MOQ 向上取整
    assert out["stock_source"] == "available"


def test_replenish_zero_when_above_reorder_point():
    out = _replenish(sku=_sku(stock=200, available=200),
                     features=_features(base_daily=1.0),
                     supplier={"lead_time_days": 10, "moq": 100})
    assert out["suggested_qty"] == 0
    assert any("无需补货" in n for n in out["notes"])


def test_replenish_uses_available_not_on_hand():
    """可用 0 / 现存 100：补货量必须按可用算（否则会少补）。"""
    out = _replenish(sku=_sku(stock=100, available=0),
                     features=_features(base_daily=10.0),
                     supplier={"lead_time_days": 10, "moq": 100})
    assert out["available_stock"] == 0
    assert out["suggested_qty"] == 400      # ceil(310 - 0 + 30) = 340 → MOQ 取整 400


def test_replenish_caps_single_order():
    out = _replenish(sku=_sku(stock=0, available=0),
                     features=_features(base_daily=100.0),
                     supplier={"lead_time_days": 10, "moq": 100})
    assert out["suggested_qty"] == 500
    assert any("上限" in n for n in out["notes"])


def test_replenish_counts_only_7d_in_transit():
    out = _replenish(sku=_sku(stock=0, available=0),
                     features=_features(base_daily=10.0),
                     in_transit=[{"qty": 20, "days_to_arrive": 3},
                                 {"qty": 999, "days_to_arrive": 30}],
                     supplier={"lead_time_days": 10, "moq": 100})
    assert out["in_transit_7d"] == 20


def test_replenish_marks_on_hand_fallback():
    out = _replenish(sku={"sku_id": "ST-01-C1-M", "stock": 100},
                     features=_features(base_daily=10.0, stock_source="on_hand"),
                     supplier={"lead_time_days": 10, "moq": 100})
    assert out["stock_source"] == "on_hand"
    assert any("人工复核" in n for n in out["notes"])


# ---------------------------------------------------------------- 输出契约
def test_skill_outputs_match_schemas():
    """`JudgeOutput`/`ReplenishOutput` 早先是零引用的死 schema，这里让它们真正校验输出。"""
    JudgeOutput.model_validate(_judge())
    ReplenishOutput.model_validate(_replenish())


@pytest.mark.parametrize("flag_case", [
    ("缺货", dict(days_of_supply=5.0, base_daily=4.0)),
    ("积压候选", dict(days_of_supply=90.0, slope_norm=0.0)),
])
def test_flags_are_strings(flag_case):
    name, features = flag_case
    judgment = _judge(features=_features(**features))
    assert name in judgment["risk_flags"]
    assert all(isinstance(f, str) for f in judgment["risk_flags"])
