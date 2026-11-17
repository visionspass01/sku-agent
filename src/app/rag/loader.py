"""知识库文档加载与清洗（RAG 链路第 1 步）。

文档格式：Markdown，顶部 `---` front matter 声明元数据。

除了原有过滤维度（品类/季节/仓/平台/生效期/版本），还声明两件**治理**相关的东西：

- `authority`：这份文档在口径冲突时的权威度——`governance`（中台治理口径，以中台字段为准）、
  `dept`（部门细则）、`general`（通用规则）、`case`（历史案例，只作参考）。
  缺省按目录推：部门文档 → `dept`，通用文档 → `general`，`case-*` → `case`。
- `key_facts`：这份文档明确规定的**可比较事实**，形如
  `key_facts: clearance_trigger_days=60天; clearance_min_discount=6折`。
  同一家公司里通用 SOP 写 60 天、女装细则写 45 天，检索会同时召回两份——
  有了 key_facts，冲突可以被**确定性**地解出来，而不是让模型在两个数字之间折中。
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import re
from pathlib import Path

from app.common.conflicts import AUTHORITY_ORDER, DEFAULT_AUTHORITY
from app.config import DEPARTMENT_CATEGORIES, KB_DIR

logger = logging.getLogger("sku-agent.rag")


@dataclasses.dataclass
class KbDocument:
    doc_id: str
    title: str
    body: str
    path: Path
    department: str = "通用"
    category: str = "全部"
    season: str = "全部"
    warehouse: str = "全部"
    platform: str = "全部"
    effective_start: dt.date | None = None
    effective_end: dt.date | None = None
    version: str = "v1"
    sha256: str = ""
    # 冲突治理（评审 2.4-2）
    authority: str = DEFAULT_AUTHORITY
    scope: str = "general"
    key_facts: list[dict] = dataclasses.field(default_factory=list)

    def meta(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "department": self.department,
            "category": self.category,
            "season": self.season,
            "warehouse": self.warehouse,
            "platform": self.platform,
            "effective_start": self.effective_start.isoformat() if self.effective_start else None,
            "effective_end": self.effective_end.isoformat() if self.effective_end else None,
            "version": self.version,
            "authority": self.authority,
            "scope": self.scope,
            "key_facts": self.key_facts,
        }

    @property
    def has_key_facts(self) -> bool:
        return bool(self.key_facts)

    def facts_for(self, section: str | None) -> list[dict]:
        """取某个**小节**适用的事实。

        为什么事实要按小节归属（复审 P2-2）：文档级事实会挂到该文档每个块上，
        于是问「清仓怎么执行」命中的是"执行节奏"小节，却也会触发「触发天数」冲突，
        提示词就要求模型先解释一个用户没问的阈值。按小节收敛后，冲突只在相关小节
        被召回时出现——不靠问句关键词匹配，结构上就对了。

        规则见 `section_matches`：`*`（或缺省）恒命中；否则与块标题**双向子串**匹配
        （声明「触发条件」可命中块标题「触发条件（女装）」）。`section=None` 只取全文档事实。
        """
        wanted = (section or "").strip()
        out: list[dict] = []
        for fact in self.key_facts:
            scope = str(fact.get("section") or "*").strip() or "*"
            if scope == "*" or (wanted and section_matches(scope, wanted)):
                out.append(fact)
        return out


def section_matches(declared: str, heading: str) -> bool:
    """小节的匹配规则：**声明名必须出现在真实标题里**（唯一实现，下发与拼写校验共用）。

    为什么是单向（复审 P2 的坑）：早先是"双向子串"，于是把 `触发条件X` 这种**拼错**
    也算命中（标题"触发条件"是声明名的子串），加载期告警永远不触发，事实静默失效。
    单向规则下：声明 `触发条件` 命中标题 `触发条件（女装）`；声明 `触发条件X` 不命中任何标题
    → 加载期就能 WARNING 出来。
    """
    declared = (declared or "").strip()
    heading = (heading or "").strip()
    if not declared or not heading:
        return False
    return declared in heading


def body_headings(body: str) -> list[str]:
    """正文里所有 Markdown 标题（去掉 `#`），用于校验 `key_facts` 声明的小节是否存在。"""
    return [re.sub(r"^#+\s*", "", line).strip()
            for line in (body or "").split("\n") if re.match(r"^#{1,6}\s+", line)]


def clean_text(text: str) -> str:
    """清洗：统一换行、去行尾空白、压缩连续空行、去掉多余分隔线。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [ln for ln in text.split("\n") if ln.strip() not in {"---", "***"}]
    return "\n".join(lines).strip()


def parse_front_matter(raw: str) -> tuple[dict, str]:
    meta: dict = {}
    body = raw
    m = re.match(r"^---\n(.*?)\n---\n?", raw, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
        body = raw[m.end():]
    return meta, body


def parse_key_facts(raw: str | None) -> list[dict]:
    """解析 `key=value; key2=value2`，数值自动拆成 (value, unit)。

    保持单行 `key: value` 的 front matter 语法（多行同名 key 会被覆盖），
    所以同一份文档的多条事实用 `;` 分隔。带单位的数字（`60天` / `6折` / `15%`）
    拆成 `{"key": ..., "value": 60.0, "unit": "天"}`，比较时只看数值。

    可选的小节前缀（用于把事实收敛到具体小节，见 `KbDocument.facts_for`）：

        key_facts: 触发条件|clearance_trigger_days=60天; 折扣阶梯|clearance_min_discount=6折

    没有前缀 / 前缀写 `*` = 全文档适用（向后兼容旧语料）。用 `|` 而不是 `:` 分隔，
    因为小节标题里常出现中文冒号（如「案例一：大促断码」）。
    """
    facts: list[dict] = []
    for part in re.split(r"[;；]", raw or ""):
        part = part.strip()
        if not part or "=" not in part:
            continue
        section = "*"
        if "|" in part.split("=", 1)[0]:
            section, part = part.split("|", 1)
            section = section.strip() or "*"
            part = part.strip()
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        m = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*(.*)$", value)
        if m:
            facts.append({"key": key, "value": float(m.group(1)),
                          "unit": m.group(2).strip(), "raw": value, "section": section})
        else:
            facts.append({"key": key, "value": value, "unit": "", "raw": value,
                          "section": section})
    return facts


def parse_triggers(raw: str | None) -> dict[str, dict[str, list[str]]]:
    """解析 `key=词1,词2; key2=词`，返回 `key → {"any": [...], "none": [...]}`。

    作用（复审 P1-1 的补完）：事实按小节收敛后，问句换一种说法（"清仓天数门槛"）
    可能召回不到事实所在的小节，冲突就静默消失了。触发词给这类问法一条**补召回**通道。

    `!` 之后是**排除词**（复审 P2-1）：

        fact_triggers: clearance_trigger_days=触发,门槛,判断标准,清仓什么时候,清仓开始!补货,折扣

    - 时间意图词（`什么时候`/`开始`）无法用子串区分"清仓什么时候开始"与
      "清仓什么时候补货"——所以时间词必须带对象（`清仓什么时候`）并配排除词（`补货`）；
    - 排除词优先：问句命中任一排除词，这条口径的触发词全部失效（回落到结构化相关性），
      避免"问补货/折扣却被要求先讲清仓阈值"这种答非所问。
    """
    triggers: dict[str, dict[str, list[str]]] = {}
    for part in re.split(r"[;；]", raw or ""):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, words = part.split("=", 1)
        key = key.strip()
        any_part, _, none_part = words.partition("!")
        any_words = [w.strip() for w in re.split(r"[,，]", any_part) if w.strip()]
        none_words = [w.strip() for w in re.split(r"[,，]", none_part) if w.strip()]
        if key and (any_words or none_words):
            triggers[key] = {"any": any_words, "none": none_words}
    return triggers


def dedupe_key_facts(facts: list[dict]) -> tuple[list[dict], list[dict]]:
    """同一份文档对同一口径声明了多个取值 → 保留第一条，其余作为"文档内部不一致"返回。

    为什么在入库前就处理（复审 P2-1）：文档级 `key_facts` 会挂到该文档每个块上，
    若同一份文档写了两个值，运行时会出现"采用 60 天（sop-x）… 不采用 45 天（sop-x）"
    这种自相矛盾的裁决——赢家和输家是同一份文档，用户只会觉得系统坏了。
    这属于**语料缺陷**，应该在加载时暴露（WARNING 日志）而不是让模型去圆。
    """
    from app.common.conflicts import Claim, values_equal

    kept: list[dict] = []
    duplicates: list[dict] = []
    by_key: dict[str, dict] = {}
    for fact in facts:
        key = str(fact.get("key") or "").strip()
        if not key:
            continue
        first = by_key.get(key)
        if first is None:
            by_key[key] = fact
            kept.append(fact)
            continue
        same = values_equal(
            Claim(key=key, value=first.get("value"), unit=str(first.get("unit") or "")),
            Claim(key=key, value=fact.get("value"), unit=str(fact.get("unit") or "")),
        )
        if not same:                     # 同值重复只是写法冗余，不算不一致
            duplicates.append(fact)
    return kept, duplicates


def default_authority(doc_id: str, department: str) -> str:
    """没写 `authority:` 时按目录推：案例 < 部门 < 通用 < 中台治理口径。"""
    if doc_id.startswith("case-"):
        return "case"
    if department and department != "通用":
        return "dept"
    return DEFAULT_AUTHORITY


def doc_fingerprint(body: str, meta: dict) -> str:
    """文档指纹 = 正文 + front matter 元数据。

    为什么不能只算正文（复审 P1-1 的连带坑）：`authority` / `key_facts` / `fact_triggers` /
    `version` 全在 front matter 里，只对正文算 sha256 时，**改了元数据但正文没动**的文档会被
    索引判成"未变化"直接跳过——库里留着旧元数据，冲突检测与权威度裁决静默失效。
    实测过：加了 `fact_triggers` 后 `update_kb.py` 报"跳过 14 篇"，触发词根本没进索引。
    """
    payload = json.dumps(meta, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(f"{payload}\n{body}".encode("utf-8")).hexdigest()


def load_documents(kb_dir: Path) -> list[KbDocument]:
    docs: list[KbDocument] = []
    for path in sorted(kb_dir.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta, body = parse_front_matter(raw)
        body = clean_text(body)
        doc_id = meta.get("doc_id") or path.stem
        start = meta.get("effective_start")
        end = meta.get("effective_end")
        department = meta.get("department", "通用")
        authority = (meta.get("authority") or "").strip().lower()
        if authority not in AUTHORITY_ORDER:
            authority = default_authority(doc_id, department)
        facts, duplicates = dedupe_key_facts(parse_key_facts(meta.get("key_facts")))
        triggers = parse_triggers(meta.get("fact_triggers"))
        for fact in facts:
            spec = triggers.get(str(fact.get("key")) or "")
            if spec:
                if spec.get("any"):
                    fact["triggers"] = list(spec["any"])
                if spec.get("none"):
                    fact["trigger_veto"] = list(spec["none"])
        if duplicates:
            logger.warning(
                "知识库语料内部口径不一致：%s 对 %s 声明了多个取值（保留 %s，忽略 %s）——请修订语料",
                doc_id,
                ", ".join(sorted({str(item.get("key")) for item in duplicates})),
                ", ".join(str(item.get("raw")) for item in facts
                         if str(item.get("key")) in {str(d.get("key")) for d in duplicates}),
                ", ".join(str(item.get("raw")) for item in duplicates),
            )
        unmatched = [fact for fact in facts
                     if (fact.get("section") or "*") != "*"
                     and not any(section_matches(str(fact.get("section")), h)
                                 for h in body_headings(body))]
        if unmatched:
            # 拼错小节名是最隐蔽的语料缺陷：文档看起来声明了口径，但事实永远挂不到任何块上，
            # 冲突检测静默失效（复审 P2）。与"重复 key"同级，必须在加载期喊出来。
            logger.warning(
                "key_facts 声明的小节在正文中不存在：%s → %s（这些事实不会被检索到，请检查小节名）",
                doc_id,
                ", ".join(f"{f.get('key')}@{f.get('section')}" for f in unmatched),
            )
        docs.append(KbDocument(
            doc_id=doc_id,
            title=meta.get("title", path.stem),
            body=body,
            path=path,
            department=department,
            category=meta.get("category", "全部"),
            season=meta.get("season", "全部"),
            warehouse=meta.get("warehouse", "全部"),
            platform=meta.get("platform", "全部"),
            effective_start=dt.date.fromisoformat(start) if start else None,
            effective_end=dt.date.fromisoformat(end) if end else None,
            version=meta.get("version", "v1"),
            sha256=doc_fingerprint(body, {
                "doc_id": doc_id, "title": meta.get("title", path.stem),
                "department": department, "category": meta.get("category", "全部"),
                "season": meta.get("season", "全部"), "warehouse": meta.get("warehouse", "全部"),
                "platform": meta.get("platform", "全部"),
                "effective_start": start, "effective_end": end,
                "version": meta.get("version", "v1"),
                "authority": authority, "scope": meta.get("scope") or "",
                "key_facts": facts, "fact_triggers": triggers,
                "raw": meta.get("key_facts") or "",
            }),
            authority=authority,
            scope=meta.get("scope") or ("dept" if department != "通用" else "general"),
            key_facts=facts,
        ))
    return docs


_COVERAGE_CACHE: dict = {"at": 0.0, "payload": {}}
_COVERAGE_TTL_SECONDS = 60.0


def kb_fact_coverage(kb_dir: Path | None = None) -> dict:
    """语料口径覆盖率（`/api/health.rag_facts` 用，60s 缓存）。

    回答的是"冲突检测到底能不能工作"：多少篇文档声明了口径、声明了多少条、
    有几个小节名写错（写错就等于该事实永远不会被检索到）。
    """
    import time

    now = time.monotonic()
    if _COVERAGE_CACHE["at"] and now - _COVERAGE_CACHE["at"] < _COVERAGE_TTL_SECONDS:
        return dict(_COVERAGE_CACHE["payload"])
    docs = load_documents(kb_dir or KB_DIR)
    facts = 0
    docs_with_facts = 0
    unmatched: list[dict] = []
    for doc in docs:
        if doc.key_facts:
            docs_with_facts += 1
        facts += len(doc.key_facts)
        headings = body_headings(doc.body)
        for fact in doc.key_facts:
            scope = str(fact.get("section") or "*")
            if scope == "*":
                continue
            if not any(section_matches(scope, h) for h in headings):
                unmatched.append({"doc_id": doc.doc_id, "key": str(fact.get("key")),
                                  "section": scope})
    payload = {
        "documents": len(docs),
        "docs_with_facts": docs_with_facts,
        "facts": facts,
        "docs_with_triggers": sum(
            1 for doc in docs if any(f.get("triggers") for f in doc.key_facts)),
        # 没声明口径的文档：这些文档永远参与不了裁决。部门文档尤其要看这一项
        # （"男装细则没声明清仓触发天数"就是靠它暴露的，复审 P1）。
        "docs_without_facts": sorted(d.doc_id for d in docs if not d.key_facts),
        # 部门级缺口（复审 P3-2）：某个部门**一篇都没声明口径**时，该部门的部门面裁决
        # 只能回落到通用口径——这是"部门例外缺一块"的根因，必须让运维看得到。
        "department_gaps": _department_gaps(docs),
        "unmatched_sections": unmatched,
        "unmatched_count": len(unmatched),
    }
    _COVERAGE_CACHE.update({"at": now, "payload": payload})
    return dict(payload)


def _department_gaps(docs) -> dict:  # noqa: ANN001 —— list[KbDocument]
    """哪个部门"一篇都没声明口径"（该部门的部门面裁决只能回落通用）。

    这是"部门例外里少了某个部门"的根因：不是裁决逻辑漏了，而是语料没声明。
    """
    gaps: dict[str, list[str]] = {}
    for department in DEPARTMENT_CATEGORIES:
        dept_docs = [d for d in docs if (d.department or "") == department]
        if dept_docs and not any(d.key_facts for d in dept_docs):
            gaps[department] = sorted(d.doc_id for d in dept_docs)
    return gaps


def reset_coverage_cache() -> None:
    """测试用：强制下次重算覆盖率。"""
    _COVERAGE_CACHE.update({"at": 0.0, "payload": {}})
