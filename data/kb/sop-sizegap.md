---
doc_id: sop-sizegap
title: 断码补全 SOP（尺码结构管理）
department: 通用
category: 全部
season: 全部
warehouse: 全部
platform: 淘系
effective_start: 2026-01-01
effective_end: 2027-12-31
version: v1.2
authority: general
scope: general
key_facts: 断码保护|broken_size_alert_days=7天; 处置优先级|broken_size_p1_hours=48小时
fact_triggers: broken_size_alert_days=预警,阈值,报警!补货,订货,采购,物流
---

# 断码补全 SOP

## 断码定义
同款商品在 5 个尺码中，有 2 个及以上尺码库存 ≤ 0，且同款近 7 天日均销量 ≥ 3 件，即判定为断码。

**与数据中台口径的对齐**：中台的 `broken_size_count` 只统计**主销码**（销量占比 ≥15%）
缺货的个数，`size_gap_rate` = 断码码数 ÷ 应有码数，`full_size_flag` 表示是否齐码。
回答时优先引用中台字段；本 SOP 的「2 个及以上尺码」只作为人工初筛条件。

## 处置优先级
1. 断码尺码中包含主销尺码（M/L/XL）时，为高优先级，48 小时内必须补货或调拨。
2. 断码尺码为非主销尺码（S/XXL）时，为一般优先级，5 个工作日内处理。
3. 断码期间，淘宝/天猫主图不得展示缺货尺码，避免流量浪费；抖音直播间不得挂断码 SKU 链接。

## 补全方式
- 优先「仓间调拨」：同款其他仓库有货时，先调拨（3 天内到仓）。
- 次优「供应商补货」：按补货 SOP 测算补货量，加急单交期按供应商加急规则执行。
- 无货可补时，做尺码归并或下架处理，并同步更新商品详情页。

## 断码保护
- 大促报名前检查断码率，断码率 = 断码 SKU 数 ÷ 报名 SKU 数，超过 20% 不得报名。
- 爆款 SKU 设置断码预警：任一主销尺码可售天数 < 7 天即报警。
