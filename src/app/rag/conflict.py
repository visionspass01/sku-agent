"""知识库口径冲突的检测与回传（RAG 链路第 5.5 步）。

场景（评审 2.4 建议里最容易被追问的一条）：
「女装清仓触发天数是几天？」——通用《清仓 SOP》写「可售天数 ≥ 60 天」，
女装部门细则写「可售天数 ≥ 45 天」。两份都在库里，检索都会召回。
没有这一层时，模型的典型行为是折中（"大约 50 天"）或把两个都念一遍，
用户拿到的口径**在任何一份制度里都不存在**。

做法：
1. 只读文档声明的 `key_facts`（结构化事实），**不猜**正文里的数字；
2. 同一 key 多个取值 → 冲突，用 `app/common/conflicts.py` 的权威度阶梯裁决
   （中台治理口径 > 部门细则 > 通用规则 > 历史案例）；
3. **不丢命中**：召回结果保持原样，只在每条上标 `is_primary=False/True`，
   并把 `## 冲突提示` 单独交给提示词——丢掉落败文档会让「女装细则」在女装问题上消失；
4. 语料里根本没填 `key_facts` 时返回 `unavailable`，**绝不说"没有冲突"**：
   没检查过 ≠ 检查过没发现，这个区别在事故复盘里很重要。
"""

from __future__ import annotations

import logging

from app.common.conflicts import (
    PRECEDENCE_TEXT,
    Claim,
    authority_label,
    detect_conflicts,
    normalize_authority,
    split_claims,
)
from app.rag.facets import claim_applicable, facets_from_filters, is_common, ordered_departments

logger = logging.getLogger("sku-agent.rag")

# 冲突检查状态：ok=发现冲突 / none=检查过无冲突 / unavailable=语料没有可比事实
STATUS_OK = "ok"
STATUS_NONE = "none"
STATUS_UNAVAILABLE = "unavailable"

# 冲突种类：跨文档（可裁决）vs 同一文档内部不一致（语料缺陷，不裁决）
KIND_CROSS_DOC = "cross_doc"
KIND_INTRA_DOC = "intra_doc"

# 计数器：这个机制的失败模式几乎都是"静默"（作用域/召回/拼错/未覆盖），
# 没有计数就只能靠猜。经 /api/health、/api/metrics 暴露（复审 P2）。
_STATS: dict[str, int] = {
    "checked": 0,      # 调用次数（含被跳过的一次检索）
    "ok": 0,           # 发现跨文档冲突
    "none": 0,         # 比对了相关口径但无分歧（含只有文档内部不一致的情况）
    "unavailable": 0,  # 没有可比/相关的口径（未做比对）
    "intra": 0,        # 文档内部不一致
    "suppressed": 0,   # 池内与问题无关、未参与比对的口径数（累计）
    "skipped": 0,      # 池内根本没有口径事实、整条冲突检查被跳过的次数
    "scope_mismatch": 0,  # 问句部门面与账号可见面完全不相交
    "scope_partial": 0,   # 问句里有账号看不见的部门（部分可见）
    "multi_department": 0,  # 多部门比较句
    "overrides": 0,    # 产出了部门例外
}


def _bump(name: str, amount: int = 1) -> None:
    if amount:
        _STATS[name] = int(_STATS.get(name, 0)) + int(amount)


def mark_skipped(reason: str = "") -> None:
    """检索侧短路（池内没有任何口径事实）也要计数（复审 P3）。

    `pipeline` 为省开销会跳过冲突检查；不计数的话 `/api/health.rag_conflicts`
    就看不出"有多少次根本没比对"——而这正是这个指标最想回答的问题。
    """
    _bump("checked")
    _bump("unavailable")
    _bump("skipped")
    if reason:
        _STATS[f"skipped:{reason}"] = int(_STATS.get(f"skipped:{reason}", 0)) + 1


def conflict_stats() -> dict:
    return dict(_STATS)


def reset_conflict_stats() -> None:
    """清零（测试隔离用）：连 `skipped:<reason>` 这类动态键一起清掉。"""
    for key in list(_STATS):
        _STATS.pop(key, None)
    _STATS.update({"checked": 0, "ok": 0, "none": 0, "unavailable": 0,
                   "intra": 0, "suppressed": 0, "skipped": 0})


def claims_of(hit) -> list[Claim]:  # noqa: ANN001 —— hybrid.Hit
    """把一条命中的 `key_facts` 变成可裁决的事实。"""
    meta = getattr(hit, "meta", None) or {}
    body = str(meta.get("body") or getattr(hit, "text", "") or "")
    snippet = body[:SNIPPET_CHARS].replace("\n", " ").strip()
    claims: list[Claim] = []
    for fact in meta.get("key_facts") or []:
        if not isinstance(fact, dict):
            continue
        key = str(fact.get("key") or "").strip()
        if not key:
            continue
        triggers = fact.get("triggers") or ()
        vetoes = fact.get("trigger_veto") or ()
        claims.append(Claim(
            key=key,
            value=fact.get("value"),
            unit=str(fact.get("unit") or ""),
            doc_id=str(meta.get("doc_id") or getattr(hit, "doc_id", "")),
            title=str(meta.get("title") or ""),
            authority=normalize_authority(meta.get("authority")),
            version=str(meta.get("version") or "v1"),
            effective_start=str(meta.get("effective_start") or ""),
            chunk_id=str(getattr(hit, "chunk_id", "")),
            triggers=tuple(str(t) for t in triggers if str(t).strip()),
            veto_triggers=tuple(str(t) for t in vetoes if str(t).strip()),
            snippet=snippet,
            section=str(meta.get("section") or fact.get("section") or ""),
            department=str(meta.get("department") or ""),
            category=str(meta.get("category") or ""),
            season=str(meta.get("season") or ""),
            warehouse=str(meta.get("warehouse") or ""),
            platform=str(meta.get("platform") or ""),
            effective_end=str(meta.get("effective_end") or ""),
        ))
    return claims


def _override_entries(claims: list[Claim]) -> list[dict]:
    """把"部门例外"（问题无部门面时被挡在裁决外的部门文档）按 (key, doc) 汇总。

    **不参与裁决、不给 winner**：它只在回答里以"若问的是 X 部门则为 Y"的形式出现。
    如果把部门文档当 winner，就会出现"管理员问男装被答成 45 天"那类错误（复审 P1）。
    """
    merged: dict[tuple[str, str], dict] = {}
    for claim in claims:
        signature = (claim.key, claim.doc_id)
        entry = merged.get(signature)
        if entry is not None:
            continue
        merged[signature] = {
            "key": claim.key,
            "doc_id": claim.doc_id,
            "title": claim.title,
            "department": claim.department,
            "display": claim.display,
            "version": claim.version,
            "authority": normalize_authority(claim.authority),
            "authority_label": authority_label(claim.authority),
        }
    return sorted(merged.values(), key=lambda item: (item["key"], item["doc_id"]))


def _intra_doc_entry(first, duplicate) -> dict:  # noqa: ANN001 —— common.conflicts.Claim
    """同一份文档对同一口径写了多个取值 → 语料缺陷（不是"谁压过谁"）。

    **防御性分支**：正常路径上 `loader.dedupe_key_facts()` 已在入库前去掉重复声明，
    索引里不会出现"同 key 双值"，所以线上几乎不会走到这里。保留它是因为：
    手工改过索引、或外部把 chunk meta 直接灌进检索器时，不能让系统退化成
    "采用 60 天（sop-x）… 不采用 45 天（sop-x）"这种自相矛盾的输出。
    """
    values = [first.display] if first is not None else []
    if duplicate.display not in values:
        values.append(duplicate.display)
    return {
        "kind": KIND_INTRA_DOC,
        "key": duplicate.key,
        "doc_id": duplicate.doc_id,
        "title": duplicate.title or (first.title if first is not None else ""),
        "values": values,
        "reason": "同一文档对同一口径声明了多个取值，需修订语料",
    }


def _merge_intra_doc(duplicates: list[Claim], kept: list[Claim]) -> list[dict]:
    """把同一文档的多个冲突取值合并成一条（按 key+doc 归并，保留全部取值）。"""
    kept_by_key = {(c.key, c.doc_id): c for c in kept}
    merged: dict[tuple[str, str], dict] = {}
    for claim in duplicates:
        key = (claim.key, claim.doc_id)
        entry = merged.get(key)
        if entry is None:
            merged[key] = _intra_doc_entry(kept_by_key.get(key), claim)
            continue
        if claim.display not in entry["values"]:
            entry["values"].append(claim.display)
    return list(merged.values())


SNIPPET_CHARS = 160


def _triggers_hit(claim: Claim, query: str, expanded: str = "") -> bool:
    """问句是否命中该口径的触发词（补召回通道，见 loader.parse_triggers）。

    **排除词只看用户原话**（复审 P2-1 的坑）：`expanded` 是加了同义词的改写结果，
    `清仓` 的同义词里有"折扣清仓"——如果拿改写结果判排除词，几乎所有清仓问题都会被
    "折扣"误杀（实测把检出率从 7/8 打到 4/8）。排除词表达的是"用户其实在问别的"，
    只能来自用户自己说的话；同义词只用来扩大**正向**匹配。

    排除词也只作用于这条触发词通道；`top-k 片段带该口径` 的结构化相关性不受影响。
    """
    if not query and not expanded:
        return False
    raw = (query or "").lower()
    for word in claim.veto_triggers:
        if word.strip() and word.strip().lower() in raw:
            return False
    if not claim.triggers:
        return False
    haystack = f"{raw} {(expanded or '').lower()}"
    return any(word.strip().lower() in haystack for word in claim.triggers if word.strip())


def _relevant(claim: Claim, top_keys: set[str], query: str, expanded: str = "") -> bool:
    """口径是否与本次问题相关：结构化（top-k 片段带该口径）或触发词命中。"""
    return claim.key in top_keys or _triggers_hit(claim, query, expanded)


def detect(hits, *, top_hits=None, query: str = "", expanded: str = "",  # noqa: ANN001
           facets=None, filters: dict | None = None,
           query_facets=None, comparative: bool = False) -> dict:
    """返回 {status, conflicts, is_primary, note, compared_keys, single_source_keys,
    department_overrides, facets, scope_notice}。

    - `hits`：**检测池**（pipeline 传 rerank 前的候选池，pool=30）——
      冲突只在"落败片段也进了池"时才可能被看见，所以检测必须在池上跑；
    - `top_hits`：真正呈现给模型的 top-k（缺省等于 `hits`）。用于判断口径**是否相关**：
      顶层片段里带该口径，或 `query` 命中该口径的触发词；
    - `query`：**用户原话**（排除词只认它）；`expanded`：加同义词后的改写结果（只用于正向匹配）；
    - `facets` / `filters`：**账号/检索面**（部门账号的可见范围，或批量 SKU 所属部门）；
    - `query_facets`：**问句部门面**（问题本身问的是哪个部门）。两者不一致时只按通用口径作答，
      并给出 `scope_notice`（规则 C3）；
    - `conflicts[*].kind`：`cross_doc`（可裁决）或 `intra_doc`（同一文档内部不一致）；
    - `department_overrides`：问题无部门面 / 多部门比较时的部门口径（只作"若为 X 部门则 Y"）。

    判定顺序（规则 C15，不能调换）：
    **部门面过滤（可比性）→ 相关性收敛 → 取值归一 → 权威度裁决 → 状态**。
    先排序后过滤正是"管理员问男装被女装细则压过"那类错误的来源。
    """
    pool = list(hits or [])
    top = list(top_hits) if top_hits is not None else pool
    if facets is None:
        facets = facets_from_filters(filters)
    asked = frozenset(query_facets or ())
    # C3：问句部门面与账号可见面**不相交** → 不能用本部门口径冒充，只能按通用口径作答
    scope_mismatch = bool(asked and facets and not (asked & facets))
    # C3 补充：问句里有账号**看不见**的部门（交集非空，但仍缺一部分）→ 部分可见。
    # 例：女装账号问「女装和男装的清仓触发天数」——交集只有女装，不能只答女装就当答完了。
    missing = frozenset(asked - facets) if (asked and facets) else frozenset()
    scope_partial = bool(missing and (asked & facets))
    # 比较意图：点名了多个部门，或者问句本身是"哪个部门/各部门/一样吗"这类比较句（复审 P2-1）
    comparison = len(asked) > 1 or bool(comparative)
    # C1 补充：多部门面 / 部分可见 / 比较意图 → 不做跨部门排序，只给通用基线 + 部门例外
    multi_facet = bool(facets and len(facets) > 1)
    common_only = scope_mismatch or multi_facet or comparison
    facet_info = {"departments": ordered_departments(facets),
                  "asked": ordered_departments(asked),
                  "missing": ordered_departments(missing),
                  "comparative": bool(comparative)}
    uncovered: list[str] = []      # 未单独规定口径、只能按通用执行的部门（下面算出来）
    notice = ""
    if scope_mismatch:
        _bump("scope_mismatch")
        notice = ("当前账号可见范围内没有「" + "、".join(ordered_departments(asked)) +
                  "」部门的口径，以下只按通用口径作答；"
                  "该部门的具体口径请向对应部门确认")
    elif scope_partial:
        _bump("scope_partial")
        notice = ("本次问句涉及多个部门，其中「" + "、".join(ordered_departments(missing)) +
                  "」不在当前账号可见范围内：以下以通用口径为基线，"
                  "可见部门的口径作为例外逐条给出，缺失部门的请向对应部门确认")
    elif multi_facet:
        _bump("multi_department")
        notice = ("本次问句涉及多个部门（" + "、".join(ordered_departments(facets)) +
                  "），各部门口径分别给出、不做单一裁决；通用口径作为基线")
    elif comparison:
        # 比较意图但只看到一个部门（典型：部门账号问「各部门一样吗」）
        _bump("comparison")
        if facets and len(facets) == 1:
            names = "、".join(ordered_departments(facets))
            notice = (f"这是**跨部门比较**的问题：当前只按「{names}」部门 + 通用口径作答；"
                      "其它部门的口径请向对应部门确认，不要据此断言「各部门一样」")
        else:
            # 全量可见（管理员）或多部门可见：不该让管理员"去问别的部门"
            notice = ("这是**跨部门比较**的问题：各部门口径分别给出、不做单一裁决，"
                      "通用口径作为基线；不要据此断言「各部门一样」")
    claims: list[Claim] = []
    for hit in pool:
        claims.extend(claims_of(hit))
    _bump("checked")
    if not claims:
        # 语料没填 key_facts，或池内没有任何可比事实：必须说"没检查"
        _bump("unavailable")
        return {"status": STATUS_UNAVAILABLE, "conflicts": [],
                "is_primary": {h.chunk_id: True for h in top},
                "compared_keys": [], "single_source_keys": [], "suppressed": 0,
                "department_overrides": [], "facets": facet_info,
                "scope_notice": notice,
                "uncovered_departments": uncovered,
                "note": (f"本次召回的 {len(pool)} 个片段均未声明可比对的口径，"
                         "冲突检查不可用（不能据此断言无冲突）")}

    top_keys = {claim.key for hit in top for claim in claims_of(hit)}
    if common_only:
        # 只让"对所有部门都适用"的口径参与裁决；部门口径降级为例外（多部门面）或直接丢弃
        # （问句问的是别的部门，本部门口径既不可比也不该出现在回答里）。
        applicable = [c for c in claims if is_common(c.department)]
        # 可见部门的口径降级为"例外"（多部门面 / 部分可见 / 比较意图）；完全不可见时不产出例外。
        exceptions = ([c for c in claims if not is_common(c.department)]
                      if (multi_facet or scope_partial or comparison) and not scope_mismatch
                      else [])
    else:
        applicable = [c for c in claims if claim_applicable(c.department, facets)]
        exceptions = [c for c in claims if not claim_applicable(c.department, facets)]
    relevant = [c for c in applicable if _relevant(c, top_keys, query, expanded)]
    # 例外只在这几种情况下呈现（规则 C2 + C1 补充）：问题没有部门面、多部门比较、
    # 部分可见的比较句、以及"跨部门比较意图"（没有点名部门但问的是部门之间谁高谁低）。
    allow_overrides = (exceptions and
                       (facets is None or multi_facet or scope_partial or comparison)
                       and not scope_mismatch)
    override_relevant = ([c for c in exceptions if _relevant(c, top_keys, query, expanded)]
                         if allow_overrides else [])
    overrides = _override_entries(override_relevant)
    if overrides:
        _bump("overrides")
    # 未覆盖部门（复审 P2-2）：只有"池内**确实召回到**该部门文档、而它没声明这个口径"才算。
    # 没召回到 ≠ 没规定——这和 `unavailable≠没冲突` 是同一条诚实性原则；
    # 早先按全局部门表算，会把"没召回"说成"未单独规定"，凭空给出制度结论。
    pool_depts = {str((h.meta or {}).get("department") or "") for h in pool}
    pool_depts = {d for d in pool_depts if d and not is_common(d)}
    claimed_depts = {c.department for c in claims if not is_common(c.department)}
    scope_depts = set(facets) if facets else pool_depts
    uncovered = ([d for d in ordered_departments(scope_depts)
                  if d in pool_depts and d not in claimed_depts]
                 if comparison else [])
    if uncovered and not scope_mismatch:
        notice = (notice + f"；另有未单独规定的部门（{'、'.join(uncovered)}）按通用口径执行"
                  if notice else
                  f"未单独规定的部门（{'、'.join(uncovered)}）按通用口径执行")
    suppressed = (len({c.key for c in claims})
                  - len({c.key for c in relevant + override_relevant}))
    if not relevant:
        # 池里有事实，但没有一个落在"问题部门面 + 相关性"内：未比对
        _bump("unavailable")
        _bump("suppressed", suppressed)
        note = ("本次召回没有与问题相关的可比口径（未做冲突比对；这不等于制度里没有冲突）"
                if not overrides else
                "本次没有与问题部门面相符的通行口径，只有部门例外（未做裁决）")
        return {"status": STATUS_UNAVAILABLE, "conflicts": [],
                "is_primary": {h.chunk_id: True for h in top},
                "compared_keys": [], "single_source_keys": [],
                "suppressed": max(suppressed, 0),
                "department_overrides": overrides, "facets": facet_info,
                "scope_notice": notice,
                "uncovered_departments": uncovered,
                "note": note}

    kept, duplicates = split_claims(relevant)
    conflicts = [dict(item, kind=KIND_CROSS_DOC) for item in detect_conflicts(kept)]
    intra = _merge_intra_doc(duplicates, kept) if duplicates else []
    compared_keys = sorted({c.key for c in kept})
    doc_counts: dict[str, set[str]] = {}
    for claim in kept:
        doc_counts.setdefault(claim.key, set()).add(claim.doc_id)
    single_source = sorted(k for k, docs in doc_counts.items() if len(docs) == 1)

    if not conflicts and not intra:
        _bump("none")
        _bump("suppressed", suppressed)
        note = (f"本次比对了 {len(compared_keys)} 个相关口径（{len(pool)} 个片段），未发现取值分歧")
        if single_source:
            note += (f"；其中 {len(single_source)} 个口径只有单一来源"
                     f"（{'、'.join(single_source[:3])}），不能据此断言制度无冲突")
        return {"status": STATUS_NONE, "conflicts": [],
                "is_primary": {h.chunk_id: True for h in top},
                "compared_keys": compared_keys, "single_source_keys": single_source,
                "suppressed": max(suppressed, 0),
                "department_overrides": overrides, "facets": facet_info,
                "scope_notice": notice,
                "uncovered_departments": uncovered,
                "note": note}

    losers: set[str] = set()
    winners: set[str] = set()
    for item in conflicts:
        winner = item.get("winner") or {}
        winners.add(str(winner.get("doc_id") or ""))
        for loser in item.get("losers") or []:
            losers.add(str(loser.get("doc_id") or ""))
    # 按**文档**标记而不是按块：同一份文档命中多个块时，只有"落败文档"才是次要口径
    primary = {
        h.chunk_id: (h.doc_id not in losers) or (h.doc_id in winners)
        for h in top
    }
    if conflicts:
        _bump("ok")
    if intra:
        _bump("intra")
    if not conflicts:
        # 只有文档内部不一致：状态是 none，计数也必须记 none，否则 resolved_rate 被低估
        _bump("none")
    _bump("suppressed", suppressed)
    note = (f"发现 {len(conflicts)} 处口径冲突，按「{PRECEDENCE_TEXT}」裁决；"
            "回答前先按胜出口径作答并说明采用了哪一份") if conflicts else ""
    if conflicts and intra:
        note = f"{note}；另有 {len(intra)} 处语料内部不一致（不要据此作答，需修订文档）"
    elif not conflicts and intra:
        note = f"语料内部不一致 {len(intra)} 处（需修订文档，暂按原文说明不确定性）"
    if conflicts and single_source:
        note += f"；另有 {len(single_source)} 个口径仅单一来源（未做交叉验证）"
    if overrides:
        note += (f"；另有 {len(overrides)} 处部门例外未参与裁决"
                 f"（如 {overrides[0].get('department')} 部门：{overrides[0].get('display')}）")
    if suppressed:
        note += f"；另有 {suppressed} 个池内口径与问题无关未参与比对"
    return {
        "status": STATUS_OK if conflicts else STATUS_NONE,
        "conflicts": conflicts + intra,
        "is_primary": primary,
        "compared_keys": compared_keys,
        "single_source_keys": single_source,
        "suppressed": max(suppressed, 0),
        "department_overrides": overrides,
        "facets": facet_info,
        "scope_notice": notice,
        "uncovered_departments": uncovered,
        "note": note,
    }


def note_text(conflicts: list[dict], overrides: list[dict] | None = None,
              notice: str = "") -> str:
    """给提示词/上下文用的 `## 冲突提示` 段落（模型照它作答，不自行折中）。

    四类内容分开写（对应规则 C13）：
    - `scope_notice`：可见范围说明（问的部门不在账号可见范围，或多部门比较）；
      必须**先**告知用户"只能按通用口径作答"，否则模型会把通用口径当成部门口径；
    - `cross_doc`：给"采用哪条 + 为什么不采用另一条"，要求模型只用一个口径；
    - `intra_doc`：同一份文档自己前后不一致，**不给采用结论**（那不是裁决问题，
      而是文档要修），只要求如实说明存在不确定性，别拿它当依据硬答；
    - `department_overrides`：问题没有部门面时被挡在裁决外的部门口径，
      **只能写成"若问的是 X 部门则为 Y"的补充句**，不能当 winner。
    """
    overrides = list(overrides or [])
    notice = (notice or "").strip()
    if not conflicts and not overrides and not notice:
        return ""
    cross = [c for c in conflicts if c.get("kind", KIND_CROSS_DOC) == KIND_CROSS_DOC]
    intra = [c for c in conflicts if c.get("kind") == KIND_INTRA_DOC]
    lines: list[str] = []
    if notice:
        lines.append("## 检索范围说明（必须先告知用户）")
        lines.append(f"- {notice}")
        lines.append("- 不要把这个通用口径说成某个部门的口径；也不要据此回答该部门的具体阈值。")
    if cross:
        lines.append("## 冲突提示（同一口径存在多份规定，按下列优先级取一条作答）")
        for item in cross:
            winner = item.get("winner") or {}
            lines.append(
                f"- 口径 `{item.get('key')}`：采用 **{winner.get('display')}**"
                f"（{winner.get('title')} · {authority_label(winner.get('authority'))}"
                f" · {winner.get('version')}）；"
                f"依据：{item.get('reason')}"
            )
            for loser in item.get("losers") or []:
                lines.append(
                    f"  - 不采用 {loser.get('display')}（{loser.get('title')} · "
                    f"{authority_label(loser.get('authority'))} · {loser.get('version')}）"
                )
            if winner.get("snippet"):
                # 胜出片段常常不在 top-k 里（它是被"池内检测 + 触发词"捞回来的），
                # 这里补上原文，模型才有依据可引，不至于只看到一个孤立数字。
                lines.append(f"  - 胜出口径原文：{winner.get('snippet')}")
        lines.append("- 不要把两个口径都列给用户，也不要折中；必要时用一句话说明为什么取这一份。")
    if intra:
        lines.append("## 语料内部不一致（不是取舍问题，是文档要修）")
        for item in intra:
            values = " / ".join(str(v) for v in (item.get("values") or []))
            lines.append(
                f"- `{item.get('key')}`：{item.get('title') or item.get('doc_id')} "
                f"同时声明了 {values}，无法判断哪条有效。"
                "请如实告知用户该口径存在文档冲突、建议确认最新修订版，不要按其中任一条硬答。"
            )
    if overrides:
        lines.append("## 部门例外（不同部门口径不同；**不参与裁决**，只作补充说明）")
        for item in overrides:
            lines.append(
                f"- `{item.get('key')}`：若问的是 **{item.get('department')}** 部门，"
                f"则为 {item.get('display')}（{item.get('title')} · "
                f"{item.get('authority_label')} · {item.get('version')}）"
            )
        if len({item.get("department") for item in overrides}) > 1:
            lines.append("- 这是**比较类问题**：各部门口径分别给出，不做单一裁决。")
        lines.append("- 主答案用「当前部门/通用」的口径；例外只能以「若问的是 X 部门则为 Y」补充，"
                     "不要把它写成已采用的口径，也不要把各部门的值都列成表格。")
    return "\n".join(lines)
