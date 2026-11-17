"""库存口径的唯一入口：**可用库存优先**（`AGENTS.md`：可用 = 现存 − 锁定 − CRM 预订 − 订单占用）。

为什么专门抽一个模块（复审 A1）：
- 研判/补货两个 Skill 早先各自读 `sku["stock"]`，而 MCP ERP 的 `stock` 是**现存库存**（on_hand），
  平台 mart 里 `available = on_hand - locked - reserved - hold` 才是治理口径——于是批量链路
  可售天数偏高、缺货漏报、补货量偏低，且与 verifier（以 Skill 输出为基准）互相打架。
- `available` 允许为 `0`：**不能**写 `int(sku.get("available") or sku.get("stock") or 0)`，
  那会把"可用为 0"吞成现存。所有调用点统一走这里，避免各处再手写一遍。
"""

from __future__ import annotations

AVAILABLE = "available"
ON_HAND = "on_hand"


def available_of(sku: dict | None) -> int:
    """可取用的库存：`available` 优先（0 是合法值）；缺字段才回落 `stock`（现存）。"""
    sku = sku or {}
    if "available" in sku and sku["available"] is not None:
        try:
            return int(sku["available"])
        except (TypeError, ValueError):
            pass
    try:
        return int(sku.get("stock") or 0)
    except (TypeError, ValueError):
        return 0


def stock_source(sku: dict | None) -> str:
    """这次用的是哪个口径：`available`（治理口径）还是 `on_hand`（回落现存，需标注偏乐观）。"""
    sku = sku or {}
    if "available" in sku and sku["available"] is not None:
        return AVAILABLE
    return ON_HAND


def on_hand_of(sku: dict | None) -> int:
    """现存库存（仅用于展示"可用/现存"对照，不参与计算）。"""
    sku = sku or {}
    try:
        return int(sku.get("stock") or 0)
    except (TypeError, ValueError):
        return 0
