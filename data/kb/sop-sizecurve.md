---
doc_id: sop-sizecurve
title: 尺码曲线与断码补全 SOP
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
key_facts: 核心概念|main_size_share=15%
---

## 什么时候用这份 SOP

用户问「哪个码卖得最好」「这个款是不是断码了」「断码怎么补」「要不要追单」时使用。
判断口径必须以数据中台的 `get_style_metrics` / `get_size_curve` / `get_size_gap` 为准。

## 核心概念

- **尺码曲线**：同一款各尺码的销量占比。占比 = 该码近 30 天销量 ÷ 同款近 30 天总销量。
- **主销码**：占比 ≥ 15% 的尺码。女装外套常见 M/L 为主销码，S/XXL 属长尾码。
- **断码**：主销码可用库存 = 0。长尾码缺货不算断码，只是「码数不全」。
- **齐码**：该款所有应有尺码的可用库存都 > 0。

## 判断步骤

1. 先取 `get_style_metrics(style_id)`，看 `size_curve` 与 `size_gap.missing_sizes`。
2. 对齐「应有尺码」（`size_count`）与「有货尺码」（`listed_size_count`）：
   差额说明有几个码没货；再看缺的是不是主销码。
3. 看 `lifecycle_stage`：
   - 上新/主推 → 断码值得补；
   - 清仓/长尾 → 不要补，改走清仓折扣（见 sop-clearance）。
4. 看 `sell_through_rate`：≥85% 说明卖得好，补货优先；<30% 且已过季，优先清货。

## 补多少（按码补，不按款摊）

- 单码缺口 = 该码日均销量 × (剩余可售天数目标 − 当前可售天数)。
- 剩余可售天数目标：主销码 30~45 天，长尾码 45~60 天（宁可慢一点也不要压货）。
- 单码补货量小于供应商 MOQ 时，优先**跨仓调拨**（`create_transfer_suggestion`，1~4 天到）；
  调拨仍不够再对供应商追单（按交期，`get_supplier_info`）。

## 输出要求

必须写清楚：断的是哪几个码（具体到 S/M/L）、是不是主销码、同款其它仓能不能调、
建议补货量（按码给）、以及是否需要人工确认（≥200 件或 ≥5 万元）。
