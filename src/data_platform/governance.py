"""数据治理层：指标口径注册 + PII 脱敏规则 + 面向 Agent 的元数据上下文。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from data_platform.db import MetaDesensitizationRule, MetaMetricDefinition

# 指标口径（单一事实来源：数据中台治理，Agent 通过 API 获取并注入 Prompt）
METRIC_DEFINITIONS: list[dict] = [
    {
        "key": "available_qty",
        "name": "可用库存",
        "formula": "可用库存 = 现存库存 − 锁定库存 − CRM 预订未发货数量 − 订单未发货占用",
        "unit": "件",
        "description": "真正可以对外承诺销售的库存，下单/补货/售罄计算都必须用可用库存，不得使用现存库存。",
        "synonyms": ["可卖库存", "能卖的库存", "可用件数"],
    },
    {
        "key": "safety_stock",
        "name": "安全库存线",
        "formula": "安全库存 = ceil(日均销量(近7天) × 供应商交期天数 × 0.3)",
        "unit": "件",
        "description": "低于安全库存线即触发补货预警；计算时先确认日均销量与交期的最新口径。",
        "synonyms": ["安全线", "安全水位", "保底库存"],
    },
    {
        "key": "reorder_point",
        "name": "再订货点",
        "formula": "再订货点 = ceil(日均销量(近7天) × (交期 + 3)) + 安全库存",
        "unit": "件",
        "description": "可用库存（含 7 天内到货在途）低于再订货点时才建议补货。",
        "synonyms": ["订货点", "触发补货点"],
    },
    {
        "key": "sell_through_days",
        "name": "预计售罄天数",
        "formula": "预计售罄天数 = (可用库存 + 7 天内到货在途) ÷ 日均销量(近7天)，日均销量为 0 时无法估算",
        "unit": "天",
        "description": "按当前动销速度还能卖多少天；≤7 天为高断货风险，8~15 天为中风险。",
        "synonyms": ["卖完天数", "还能卖几天", "售罄时间"],
    },
    {
        "key": "sales_rate",
        "name": "动销率",
        "formula": "动销率 = 近 30 天有销量的 SKU 数 ÷ 有货 SKU 总数",
        "unit": "%",
        "description": "衡量商品活跃度；单 SKU 维度可看近 30 天是否有销量（sold_30d_flag）。",
        "synonyms": ["卖得动不动", "活跃率", "动销比"],
    },
    {
        "key": "daily_avg_7d",
        "name": "日均销量（近7天）",
        "formula": "日均销量 = 近 7 天出库销量合计 ÷ 7",
        "unit": "件/天",
        "description": "所有库存与补货计算统一使用该口径，不使用 30 天均值。",
        "synonyms": ["日均销", "日销"],
    },
    {
        "key": "in_transit_qty",
        "name": "在途采购数量",
        "formula": "在途采购数量 = 已下单未到仓的采购单数量之和（可选按 7 天内到货过滤）",
        "unit": "件",
        "description": "判断断货风险与补货时必须叠加在途，避免重复补货。",
        "synonyms": ["在途", "在途量", "未到货数量"],
    },
    {
        "key": "stockout_risk",
        "name": "缺货风险等级",
        "formula": "预计售罄天数 ≤ 7 天为高风险；8~15 天为中风险；否则低风险",
        "unit": "等级",
        "description": "结合在途与 CRM 预订计算；高风险需输出影响评估并建议补货/调拨。",
        "synonyms": ["断货风险", "缺货风险"],
    },
    # ---- 服装行业专有口径（评审 2.1-5 / 3.5）----
    # 这些指标在通用电商里没有，但服装的补货/清仓决策全靠它们：
    # SKU 级"库存充足"很可能是「断码压货」——某几个主销码卖完了，其余码还压着。
    {
        "key": "size_curve_share",
        "name": "尺码销量占比（尺码曲线）",
        "formula": "尺码占比 = 该码近 30 天销量 ÷ 同款近 30 天总销量；≥15% 视为主销码",
        "unit": "%",
        "description": ("回答「这个款哪些码最好卖」。补货/断码判断都必须按码看，"
                        "不能按款或按 SKU 平均——M/L 卖爆而 S/XXL 压货是最常见的形态。"),
        "synonyms": ["尺码结构", "码数占比", "主销码", "尺码曲线"],
    },
    {
        "key": "main_size_flag",
        "name": "主销码标记",
        "formula": "主销码 = 该码近 30 天销量占同款 ≥ 15%",
        "unit": "标记",
        "description": "主销码缺货才叫断码；长尾码缺货通常不影响销售，别一律当断码处理。",
        "synonyms": ["主码", "主力尺码"],
    },
    {
        "key": "full_size_rate",
        "name": "齐码率",
        "formula": "齐码率 = 全码可用库存 > 0 的款数 ÷ 在售款数",
        "unit": "%",
        "description": ("款级指标：断一个码就不算齐码。齐码率低说明上架结构不健康，"
                        "补货要按「补齐尺码」而不是「加总量」。"),
        "synonyms": ["齐码", "全码率", "码数齐全度"],
    },
    {
        "key": "broken_size_rate",
        "name": "断码率",
        "formula": "断码率 = 主销码可用库存 = 0 的款数 ÷ 在售款数",
        "unit": "%",
        "description": "与齐码率互补但不等价：长尾码缺货不计入断码率。断码率高时优先调拨/追单，而不是整款再进货。",
        "synonyms": ["断码", "缺码", "断码款占比"],
    },
    {
        "key": "size_gap_count",
        "name": "断码尺码数",
        "formula": "断码尺码数 = 该款可用库存 ≤ 0 的主销码个数",
        "unit": "个",
        "description": "定位到「哪几个码断了」；回答里要给出具体尺码，而不是只说「断码」。",
        "synonyms": ["断几个码", "缺哪几个码"],
    },
    {
        "key": "turnover_days",
        "name": "库存周转天数",
        "formula": "库存周转天数 = 可用库存 ÷ 日均销量（近 30 天口径）",
        "unit": "天",
        "description": ("衡量库存健康度（30 天口径），与「预计售罄天数」（7 天口径、看断货风险）"
                        "是两个指标，不要混用；周转天数高且销量下滑 = 积压信号。"),
        "synonyms": ["周转天数", "库存周转", "库存健康度"],
    },
    {
        "key": "sell_through_rate",
        "name": "售罄率",
        "formula": "售罄率 = 近 30 天销量 ÷ (近 30 天销量 + 当前可用库存)",
        "unit": "%",
        "description": "判断首单是否要追单：售罄率高（≥85%）说明卖得好，优先追单；低则考虑清仓。",
        "synonyms": ["售罄", "售罄比例", "消化率"],
    },
    {
        "key": "lifecycle_stage",
        "name": "款式生命周期阶段",
        "formula": ("上新（上架 ≤14 天）/ 清仓（售罄率 ≥85% 或上架 >120 天）/ "
                    "长尾（上架 >180 天且售罄率 <20%）/ 主推（其余）"),
        "unit": "阶段",
        "description": ("决定用哪套 SOP：补货 SOP 与清仓 SOP 的策略互相冲突，"
                        "阶段由数据中台给出，不要让模型自己猜。"),
        "synonyms": ["生命周期", "款式阶段", "上新/主推/清仓"],
    },
    {
        "key": "competitor_price_band",
        "name": "竞品价格带",
        "formula": "同款竞品价格的最低 / 中位 / 均价 / 最高",
        "unit": "元",
        "description": "定价与清仓折扣的参照；本款价格显著高于竞品中位时要先解释差异，不要直接打折。",
        "synonyms": ["价格带", "竞品价格", "比价"],
    },
]

# 尺码曲线 / 断码之外，服装补货还会用到这些「宽度 × 深度」口径（写进 Agent 提示词的口径摘要）
APPAREL_NOTES: list[str] = [
    "按码补货：补货量按「主销码缺口」算，不要把整款总量平均摊到所有码。",
    "断码优先调拨：同款其它仓有货时先调拨（最快 1~4 天），再考虑供应商追单（按交期）。",
    "清仓与补货互斥：生命周期为清仓/长尾的款不做补货，改走清仓折扣 SOP。",
]

# 检索同义词（业务知识，归治理）：RAG 的查询改写走这张表，而不是硬编码在 Agent 里。
# 改词表不用发 Agent 版本——Agent 启动时拉一次，之后按 TTL 刷新。
QUERY_SYNONYMS: dict[str, list[str]] = {
    "断码": ["缺码", "尺码断货", "尺码不足", "码不全"],
    "齐码": ["全码", "码数齐全", "码全"],
    "补货": ["订货", "采购", "补单", "追单"],
    "翻单": ["追单", "返单", "二次下单"],
    "清仓": ["清库", "甩货", "折扣清仓", "特卖"],
    "积压": ["滞销", "库存积压", "压货"],
    "退货率": ["退货", "售后率", "退换率"],
    "大促": ["双11", "双十一", "618", "年货节", "活动", "预售"],
    "换季": ["季末", "季节切换", "过季"],
    "缺货": ["断货", "无货", "售罄"],
    "调拨": ["仓间调拨", "转仓", "调货"],
    "交期": ["到货时间", "供应商交期", "货期"],
    "尺码曲线": ["尺码结构", "码数占比", "主销码"],
    "生命周期": ["上新", "主推", "清仓", "长尾", "过季"],
    "周转": ["周转天数", "库存周转", "库存健康度"],
    "售罄率": ["售罄", "消化率", "卖完比例"],
}

DESENSITIZATION_RULES: list[dict] = [
    {"field": "customer_name", "rule": "保留姓氏首字，其余用 * 替代", "example": "张*"},
    {"field": "phone", "rule": "保留前 3 位与后 4 位，中间用 **** 替代", "example": "138****1234"},
    {"field": "receiver/address", "rule": "仅保留市级区域，其余地址段落打码", "example": "浙江省杭州市***"},
]


def seed_governance(session: Session) -> None:
    for item in METRIC_DEFINITIONS:
        session.merge(MetaMetricDefinition(
            key=item["key"], name=item["name"], formula=item["formula"],
            unit=item["unit"], description=item["description"],
            synonyms=list(item["synonyms"]), owner="数据治理组", version="v1",
        ))
    for rule in DESENSITIZATION_RULES:
        session.merge(MetaDesensitizationRule(
            field=rule["field"], rule=rule["rule"], example=rule["example"],
        ))


def metadata_prompt_text() -> str:
    """把治理元数据压缩成 Agent 系统提示片段。"""
    lines = ["## 数据中台治理口径（回答库存/售罄/补货问题必须使用以下口径）"]
    for item in METRIC_DEFINITIONS:
        syn = "、".join(item["synonyms"])
        lines.append(f"- 【{item['name']}】{item['formula']}（单位：{item['unit']}；同义词：{syn}）")
    lines.append("\n## 服装行业补货纪律")
    for note in APPAREL_NOTES:
        lines.append(f"- {note}")
    lines.append("\n## 脱敏规则（涉及客户信息时只能引用脱敏后的值）")
    for rule in DESENSITIZATION_RULES:
        lines.append(f"- {rule['field']}：{rule['rule']}（示例：{rule['example']}）")
    return "\n".join(lines)
