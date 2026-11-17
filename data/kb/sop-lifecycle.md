---
doc_id: sop-lifecycle
title: 款式生命周期与运营动作 SOP
department: 通用
category: 全部
season: 全部
warehouse: 全部
platform: 全部
effective_start: 2026-01-01
effective_end: 2027-12-31
version: v1
authority: governance
scope: general
key_facts: *|lifecycle_new_days=14天; *|lifecycle_clearance_sell_through=85%
---

## 为什么要先判生命周期

补货与清仓的策略互相冲突：补货要加库存，清仓要减库存。
同一个款如果阶段判错，动作就会反向——所以阶段由数据中台给出（`get_style_metrics` 的
`lifecycle_stage`），不要自己按「感觉新不新」判断。

## 阶段判定（中台口径）

| 阶段 | 条件 | 运营动作 |
|---|---|---|
| 上新 | 上架 ≤ 14 天 | 观察为主，看首周售罄率；不急于追单，避免误判 |
| 主推 | 售罄率 < 85% 且上架 ≤ 120 天 | 补货 / 追单 / 报活动；断码优先补齐主销码 |
| 清仓 | 售罄率 ≥ 85% 或 上架 > 120 天 | 停止补货，走折扣阶梯（见 sop-clearance） |
| 长尾 | 上架 > 180 天且售罄率 < 20% | 停止补货，并处理尾货（并柜 / 打包 / 直播清仓） |

## 判断步骤

1. 取 `get_style_metrics(style_id)`：`lifecycle_stage` / `days_since_launch` /
   `sell_through_rate` / `turnover_days`。
2. 交叉验证：周转天数高 + 销量下滑 = 积压信号，即使还在「主推」期也要提示风险。
3. 看季节：秋冬款在 3~8 月进入季末，春夏款在 9~2 月进入季末；
   季末的主推款应提前 30 天规划清仓，而不是等售罄率掉下来。
4. 看大促：`get_platform_activities` 显示 30 天内有活动窗口时，主推款可提前备货，
   但清仓/长尾款必须优先报名清仓活动。

## 输出要求

先给阶段结论，再给动作建议，最后说明依据（售罄率 / 上架天数 / 周转天数）。
阶段为清仓或长尾时，**不要**输出补货建议。
