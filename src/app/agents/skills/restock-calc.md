# restock-calc

> 补货测算与补货单创建：先算安全库存线与再订货点，再决定数量、是否走人工确认。

## 适用场景
- 用户问「要不要补货 / 补多少 / 帮我下补货单」。

## 计算口径（与 `src/app/skills/replenishment.py` 完全一致，不要自己另发明公式）
- 日均销量 = 近 7 天销量 ÷ 7
- 安全库存 = ceil(日均 × 交期 × 0.3)
- 再订货点 = ceil(日均 × (交期 + 3)) + 安全库存
- 目标覆盖天数 = 交期 + 21
- **先判短路**：可用库存 + 7 天内到途 ≥ 再订货点 → 不补货（避免"已高于再订货点还建议补货"的自相矛盾）。
- 建议补货量 = ceil(目标覆盖天数 × 日均 − 可用库存 − 7 天内到途 + 安全库存)；按 MOQ 处理：低于 MOQ 按 MOQ，否则向上取到 MOQ 的整数倍；**单次上限 500 件**（超出截断并说明分批）。

## 步骤
1. `get_sku_metrics` 取安全库存、再订货点、交期、MOQ；`get_inventory` 取**可用库存**；`get_sales` 取近 7 天日均；`get_in_transit` 取在途与到货日。
2. 按上面的口径算一遍，**并优先采用 Skill/工具的 `suggested_qty`**（若两者不一致，以工具输出为准并在回答里说明差异原因）。
3. 调 `create_replenishment_order` 创建草稿单（幂等键由系统按 会话+轮次+SKU+数量+原因 计算，**不要自己传幂等键**）。
4. 返回 `pending_confirmation` 时必须说明「已生成草稿单，等待人工确认」并给出 `approval_id` / `po_no`，**不得声称已审批通过**。

## 取数工具
- `get_sku_metrics`、`get_inventory`、`get_sales`、`get_in_transit`、`create_replenishment_order`

## 输出模板
1) 测算过程（日均/交期/安全库存/再订货点，全部标注 result_id）；2) 建议数量与理由（含 MOQ/上限说明）；3) 草稿单状态（approval_id / po_no）；4) 若进入人工确认，提示到「补货单列表」处理。
