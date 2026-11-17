# size-gap-diagnosis

> 同款多色多码断码诊断：定位「哪些**主销码**断了、断码是否影响转化、补哪个尺码最划算」。

## 适用场景
- 用户问「这个款是不是断码了 / M 码没货影响多大 / 同款其他尺码卖得怎么样」。

## 判定口径（与 `AGENTS.md`、`get_style_metrics`、库存研判 Skill 同源）
- **主销码**：该码近 30 天销量 ÷ 同款近 30 天总销量 **≥ 15%**（以 `get_size_curve` / `get_style_metrics` 的尺码曲线为准）。
- **断码 = 主销码的可用库存为 0**；长尾码（占比 <15%）缺货**不算断码**，只能作为「同款另有 N 个码缺货」的说明信息。
- **齐码** = 所有应有尺码的可用库存都 > 0（款级指标，别用"某个码有货"代替）。
- 可用库存 = 现存 − 锁定 − CRM 预订 − 订单占用（`get_inventory` 的 `available`；若返回 `stock_source="on_hand"` 说明缺字段、按现存估算，要标注"偏保守"）。

## 步骤
1. `get_style_metrics(style_id)` 拿款级信息：`lifecycle_stage`、尺码曲线、齐码/断码、售罄率、周转天数；确认 `size_semantics`（`main_size` 才代表有主销码标记）。
2. `get_size_curve(style_id)` 拿各码 30 天销量占比 → 圈出主销码（≥15%）。
3. 对主销码逐个 `get_inventory` 取可用库存；`get_sales` 取近 7 天日均，用于估算缺货影响。
4. 结论：
   - 本 SKU 的码是主销码且可用为 0 → **本码断码**（要点名具体码）；
   - 只是长尾码缺货 → **不算断码**，最多说"同款另有 N 个码缺货"；
   - 断码优先看调拨：`create_transfer_suggestion`（同款其它仓有货，1~4 天到）→ 再考虑供应商追单。
5. 补码建议：按**主销码缺口**算数量（不要把钱平均摊到所有码），并说明是否受 MOQ 限制。

## 取数工具
- `get_style_metrics`、`get_size_curve`、`get_inventory`、`get_sales`、`get_in_transit`、`get_sku_metrics`、`create_transfer_suggestion`

## 输出模板
1) 断码结论（哪个**主销码**断了；长尾码缺货单独说明）；2) 影响量化（该码近 7 天日均 × 交期 = 缺货损失估算）；3) 补码优先级表（尺码/建议数量/依据 result_id）。

## 与初筛的区别（别混用）
`sku-screening` / 批量巡检用的是**候选初筛口径**（同款零库存码数 ≥ 2），只用来"挑出值得深看的款"，
**不能作为断码结论**。对外下结论一律用本文档的主销码口径。
