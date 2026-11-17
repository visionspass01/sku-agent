# weekly-ops-review

> 周度运营复盘：把「上周巡检结论 + 本周风险变化 + 待办」组织成一页简报，可直接贴群里。

## 适用场景
- 用户问「出一份上周复盘 / 这周该做什么 / 帮我把巡检结果汇总」。

## 步骤
1. 先查 L3 组织记忆：`search_analyses` 找上周同部门分析（关键词：周报/复盘/巡检），
   有则只取结论与关键指标作为基线，**不要重读上周对话**。
2. 再看本周变化：`get_sales_rate(department)` 取动销率，`search_analyses` 对比上周数值。
3. 汇总三类信号：新增风险（缺货/断码）、好转项、持续积压项。
4. 生成待办：每条待办绑定 SKU 与责任部门，并注明数据来源 result_id。
5. 若用户要求留档，调用 `save_analysis` 写入组织记忆（标题规范：[部门] 周度复盘 - 起止日期 - 结论摘要）。

## 取数工具
- `search_analyses`、`get_sales_rate`、`get_inventory`、`get_sell_through`、`save_analysis`

## 输出模板
一页四段：① 本周结论（3 句以内）② 关键指标表（本周 vs 上周）③ 待办清单（含 SKU）④ 数据来源（result_id 列表）。
