"""事实冲突的通用判定与优先级裁决（知识库 / 分析档案共用）。

为什么需要一层"通用"的裁决（评审 2.4-2）：
同一家公司里，「清仓触发天数」在通用 SOP 里写 60 天、在女装部门细则里写 45 天，
两份都是真实存在的制度。检索会把两份都召回，模型看到两个数字，于是**折中**（比如答"50 天"）
或者把两个都念一遍——这是最糟的结果：用户拿到的口径在两份文档里都不存在。

所以冲突必须**显式解出来**，而不是留给模型猜：
1. 先把文档变成「可比较的事实」（`key=value` 三元组）；
2. 同一个 key 出现多个不同取值 → 记为一次冲突；
3. 按**权威度**裁决：中台治理口径 > 部门细则 > 通用规则 > 历史案例；
   同权威度再比版本号、再比生效日期（新的胜），最后按 doc_id 去重保证可复现。

本模块只做"裁决"，不关心事实来自知识库还是分析档案（`app/rag/conflict.py` 是前者的适配层）。

**重要**：冲突**不丢文档**。召回结果保持原样（否则问"女装清仓"可能把女装细则丢掉），
只在每条命中上标 `is_primary`，并把裁决结果单独回传，让提示词与前端都能看到"为什么取这条"。
"""

from __future__ import annotations

import dataclasses
import re
from typing import Iterable

# 权威度阶梯：数字越小越优先。顺序就是业务口径的优先级，不要随手改。
AUTHORITY_ORDER: tuple[str, ...] = ("governance", "dept", "general", "case")
AUTHORITY_LABEL: dict[str, str] = {
    "governance": "中台治理口径",
    "dept": "部门细则",
    "general": "通用规则",
    "case": "历史案例",
}
DEFAULT_AUTHORITY = "general"
PRECEDENCE_TEXT = "中台治理口径 > 部门细则 > 通用规则 > 历史案例"


def authority_label(authority: str) -> str:
    return AUTHORITY_LABEL.get(normalize_authority(authority), authority or "")


def normalize_authority(authority: str | None) -> str:
    value = (authority or "").strip().lower()
    return value if value in AUTHORITY_ORDER else DEFAULT_AUTHORITY


def authority_rank(authority: str | None) -> int:
    """未知权威度排在已知的后面（不丢文档，但也不会抢走已知更权威的结论）。"""
    value = (authority or "").strip().lower()
    return AUTHORITY_ORDER.index(value) if value in AUTHORITY_ORDER else len(AUTHORITY_ORDER)


def version_key(version: str | None) -> tuple[int, ...]:
    """`v1.2` / `1.2.0` / `v0.9` → 可比较元组；解析不出数字的当作最老版本。"""
    nums = re.findall(r"\d+", version or "")
    return tuple(int(n) for n in nums) if nums else (0,)


# 单位别名：同一口径常被写成不同单位（"60 天"/"60 日"、"48 小时"/"48h"），
# 不归一就会造出**假冲突**——提示词被迫二选一，输出"采用 60 天…不采用 60 日"这种
# 自相矛盾的说明（复审 P1-3）。只归一「同义单位」，不做单位换算（天↔小时不换算：
# 那属于口径错误，应当暴露而不是悄悄抹平）。
UNIT_ALIASES: dict[str, str] = {
    "天": "天", "日": "天",
    "小时": "小时", "h": "小时", "H": "小时", "hr": "小时",
    "分钟": "分钟", "min": "分钟",
    "折": "折", "%": "%", "％": "%",
    "件": "件", "元": "元", "万元": "万元", "次": "次", "人": "人", "个": "个", "%点": "%",
}


def canonical_unit(unit: str | None) -> str:
    """单位归一（未知单位原样返回，避免把没见过的单位强行合并）。"""
    text = str(unit or "").strip()
    return UNIT_ALIASES.get(text, text)


def normalize_value(value, unit: str = "") -> str:  # noqa: ANN001
    """取值归一：`60` / `60.0` / `"60天"` / `"60 日"` 的数值与单位都要能对齐。"""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return f"{float(value):g}{canonical_unit(unit)}"
    text = str(value or "").strip()
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*(.*)", text)
    if m:
        return f"{float(m.group(1)):g}{canonical_unit(m.group(2) or unit)}"
    return text


def _numeric_part(normalized: str) -> float | None:
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(.*)", normalized or "")
    return float(m.group(1)) if m else None


def _unit_part(normalized: str) -> str:
    m = re.fullmatch(r"[+-]?\d+(?:\.\d+)?(.*)", normalized or "")
    return (m.group(1) or "") if m else ""


def values_equal(a: "Claim", b: "Claim") -> bool:
    """两个取值是否表达同一个事实（单位同义 / 百分比与小数互认）。"""
    na, nb = a.normalized, b.normalized
    if na == nb:
        return True
    ua, ub = _unit_part(na), _unit_part(nb)
    va, vb = _numeric_part(na), _numeric_part(nb)
    if va is None or vb is None:
        return False
    if ua != ub:
        # 百分号 vs 小数：`15%` 与 `0.15` 是同一个阈值；只接受 (0,1) 区间，
        # 免得把「0.5 折」当成 50%。
        if {ua, ub} == {"%", ""}:
            pct, plain = (va, vb) if ua == "%" else (vb, va)
            return 0 < abs(plain) < 1 and abs(pct) == abs(plain) * 100
        return False
    return va == vb


@dataclasses.dataclass
class Claim:
    """一条可比较的事实：谁（doc）在什么权威度下，把 key 定成了什么值。

    `triggers` / `snippet` / `section` 服务于"冲突展示"（复审 P1-1）：
    - `triggers`：问句命中这些词时，即使该口径的片段没进 top-k 也参与比对（补召回）；
    - `snippet`：胜出/落败片段的原文摘要——胜出片段常常不在 top-k，模型需要依据正文；
    - `section`：声明该口径的小节，用于诊断与展示。
    三者都不参与取值比较，`normalized` 只看 value+unit。
    """

    key: str
    value: object
    unit: str = ""
    doc_id: str = ""
    title: str = ""
    authority: str = DEFAULT_AUTHORITY
    version: str = "v1"
    effective_start: str = ""
    chunk_id: str = ""
    note: str = ""
    triggers: tuple[str, ...] = ()
    veto_triggers: tuple[str, ...] = ()
    snippet: str = ""
    section: str = ""
    # 适用面（仲裁前的可比性闸门）：部门是主维度，其余留给后续扩展
    department: str = ""
    category: str = ""
    season: str = ""
    warehouse: str = ""
    platform: str = ""
    effective_end: str = ""

    @property
    def normalized(self) -> str:
        # 单位一起参与归一：`60` + 天 与字符串 `"60天"` / `"60 日"` 必须是同一个取值
        return normalize_value(self.value, self.unit)

    @property
    def display(self) -> str:
        raw = str(self.value)
        if isinstance(self.value, float) and self.value.is_integer():
            raw = str(int(self.value))          # 60.0 天 → 60 天（别把浮点尾巴念给用户）
        elif isinstance(self.value, int):
            raw = str(self.value)
        if self.unit and not raw.endswith(self.unit):
            return f"{raw} {self.unit}"
        return raw

    def to_dict(self) -> dict:
        return {
            "key": self.key, "value": self.value, "unit": self.unit,
            "display": self.display,
            "doc_id": self.doc_id, "title": self.title,
            "authority": normalize_authority(self.authority),
            "authority_label": authority_label(self.authority),
            "version": self.version, "effective_start": self.effective_start,
            "chunk_id": self.chunk_id,
            "section": self.section,
            "snippet": self.snippet,
            "department": self.department,
        }


class _Desc:
    """（已弃用）降序包装：仅保留给历史调用，新代码用 `precedence_key` 的数值取反。

    教训：自定义比较类要同时实现 `__eq__`——`sorted` 的元组比较先用 `==` 找第一处不同，
    只实现 `__lt__` 时 `(rank, _Desc(x), ...) == (rank, _Desc(x), ...)` 恒为 False，
    排序会退化成"保持原序"，版本号/生效日这些靠后的裁决条件全部失效。
    """

    __slots__ = ("value",)

    def __init__(self, value) -> None:  # noqa: ANN001
        self.value = value

    def __eq__(self, other: object) -> bool:  # noqa: D105
        return isinstance(other, _Desc) and self.value == other.value

    def __lt__(self, other: "_Desc") -> bool:  # noqa: D105
        return other.value < self.value


def _date_int(value: str | None) -> int:
    """`2026-01-01` → 20260101；缺失按 0（同权威度下"没说生效期"排在最后）。"""
    digits = re.sub(r"\D", "", str(value or ""))
    return int(digits[:8]) if digits else 0


def precedence_key(claim: Claim) -> tuple:
    """裁决排序键（**升序即优先**）：权威度 → 版本新 → 生效期新 → doc_id → chunk_id。

    统一用「数值取反」表达降序，避免自定义比较对象在元组排序里退化成保持原序。
    """
    version = (version_key(claim.version) + (0, 0, 0, 0))[:4]
    return (
        authority_rank(claim.authority),
        tuple(-n for n in version),
        -_date_int(claim.effective_start),
        claim.doc_id,
        claim.chunk_id,
    )


def rank_claims(claims: Iterable[Claim]) -> list[Claim]:
    return sorted(claims, key=precedence_key)


def _reason(winner: Claim, loser: Claim) -> str:
    """一句话解释"为什么取这条"——提示词要把它写进回答，审计也要看得懂。"""
    win_rank, lose_rank = authority_rank(winner.authority), authority_rank(loser.authority)
    if win_rank != lose_rank:
        return (f"{authority_label(winner.authority)}优先于"
                f"{authority_label(loser.authority)}（{PRECEDENCE_TEXT}）")
    if version_key(winner.version) != version_key(loser.version):
        return f"同为{authority_label(winner.authority)}，版本更新（{winner.version} > {loser.version}）"
    if str(winner.effective_start or "") != str(loser.effective_start or ""):
        return "同权威度同版本，按生效日期取新的"
    return f"同权威度同版本同生效日，按文档 id 确定性取定（{winner.doc_id}）"


def split_claims(claims: Iterable[Claim]) -> tuple[list[Claim], list[Claim]]:
    """按 (key, doc_id) 折叠，返回 (保留, 文档内部取值不一致的重复项)。

    区分两种"同一文档多条"：
    - 同一份文档的多个块带同一条文档级事实（值相同）→ 只是重复，静默丢弃；
    - 同一份文档对同一口径写了**不同**取值 → 语料缺陷，单独返回给上层暴露，
      不能进入跨文档裁决（否则赢家和输家是同一份文档，提示会自相矛盾）。
    """
    kept: list[Claim] = []
    duplicates: list[Claim] = []
    by_key: dict[tuple[str, str], Claim] = {}
    for claim in claims:
        if not claim.key:
            continue
        signature = (claim.key, claim.doc_id)
        first = by_key.get(signature)
        if first is None:
            by_key[signature] = claim
            kept.append(claim)
            continue
        if not values_equal(first, claim):
            duplicates.append(claim)
    return kept, duplicates


def _all_equal(items: list[Claim]) -> bool:
    """以首条为基准，判断一组取值是否表达同一个事实（含单位同义/百分比互认）。"""
    if len(items) < 2:
        return True
    head = items[0]
    return all(values_equal(head, other) for other in items[1:])


def detect_conflicts(claims: Iterable[Claim]) -> list[dict]:
    """同一 key 出现 ≥2 个**不同**取值 → 一次冲突，并给出 win 家与理由。

    同一篇文档的多个块都会带上文档级 `key_facts`，所以先按 (key, doc, 取值) 去重，
    保留**排在前面**的那条（调用方按相关性排序，等于保留最相关块）。
    否则一份 SOP 命中 3 个块，"落败方"就会在同一份文档里重复出现 3 次。

    "不同取值"用 `values_equal` 判：`60天` 与 `60日`、`15%` 与 `0.15` 都不算冲突。
    """
    grouped: dict[str, list[Claim]] = {}
    seen: set[tuple[str, str, str]] = set()
    for claim in claims:
        if not claim.key:
            continue
        signature = (claim.key, claim.doc_id, claim.normalized)
        if signature in seen:
            continue
        seen.add(signature)
        grouped.setdefault(claim.key, []).append(claim)

    conflicts: list[dict] = []
    for key, items in grouped.items():
        if _all_equal(items):
            continue                     # 取值一致：不是冲突
        ranked = rank_claims(items)
        winner = ranked[0]
        losers = ranked[1:]
        conflicts.append({
            "key": key,
            "winner": winner.to_dict(),
            "losers": [c.to_dict() for c in losers],
            "reason": _reason(winner, losers[0]),
            "values": [c.to_dict() for c in ranked],
        })
    conflicts.sort(key=lambda c: c["key"])
    return conflicts
