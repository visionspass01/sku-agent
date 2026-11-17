"""L1 会话记忆：事实卡（`<analysis-facts>`）+ 工作集（`<working-set>`）。

设计要点：
- 事实卡 = 「可复现结论骨架」：只留结论句 + 数值 + result_id + as_of，不留流水行；
  默认**纯规则抽取**（`memory_facts_use_llm=False`），保证同样输入得到同样卡片。
- 工作集 = 实体表（SKU / 款号 / 尺码 / 颜色 / 品类 / 指标 / 时间窗），**每轮确定性追加**，
  用来解决「这个款、它、刚才那个尺码」这类指代。
- 压缩策略：历史超过 `chat_history_limit` 条以后，早期消息被事实卡替代，最近若干条保留原文
  （等价 dsh-compaction-basic 的 retainRatio）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from sqlalchemy import select

from app.config import get_settings
from app.db.models import ChatMemory, ChatMessage, ChatQueryPlan
from app.memory.prompt_safety import escape_tag_text
from app.memory.prune import estimate_tokens

# 工具结果 envelope 里的 `summary` 字段（形如 `get_inventory：available=693；on_hand=700`）
_SUMMARY_FIELD_RE = re.compile(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"')
# 从摘要里抽 `key=value`（值是数字或短标识符），用于事实卡的可读呈现
_SUMMARY_PAIR_RE = re.compile(r"([a-z_]{2,})=([^\s；;，,）)]+)")

SKU_RE = re.compile(r"\bST-\d{2}-C\d{1,2}-(?:[SMLX]{1,3}|\d{2,3})\b", re.I)
STYLE_RE = re.compile(r"\bST-\d{2}\b", re.I)
SIZE_RE = re.compile(r"(?<![A-Za-z0-9])(XXL|XL|XS|S|M|L|\d{2,3}码)(?![A-Za-z0-9])")
COLOR_RE = re.compile(r"([\u4e00-\u9fff]{1,2}色)")
TIME_RE = re.compile(r"(近\s*\d+\s*(?:天|日|小时|周|月)|上周|本周|本月|上月|昨天|今天|换季前\s*\d+\s*天)")
METRIC_WORDS = (
    "可用库存", "现存", "在途", "预订", "待发货", "售罄天数", "售罄", "日均销量", "动销率",
    "退货率", "断码", "缺货", "积压", "滞销", "安全库存", "再订货点", "补货量", "金额",
)
DEPARTMENTS = ("男装", "女装", "童装")

_CTX_OPEN, _CTX_CLOSE = "<analysis-facts", "</analysis-facts>"


# ---------------------------------------------------------------- 存取
def ensure_memory(session, session_id: str) -> ChatMemory:
    row = session.get(ChatMemory, session_id)
    if row is None:
        now = dt.datetime.now()
        row = ChatMemory(session_id=session_id, facts_card="", facts_version=0, facts_hash="",
                         facts_trigger="init", facts_as_of=None, working_set={},
                         working_version=0, updated_at=now)
        session.add(row)
        session.flush()
    return row


def get_memory_snapshot(session, session_id: str) -> dict | None:
    row = session.get(ChatMemory, session_id)
    if row is None:
        return None
    return {
        "facts_card": row.facts_card or "",
        "facts_version": row.facts_version or 0,
        "facts_trigger": row.facts_trigger or "",
        "facts_as_of": row.facts_as_of.isoformat(timespec="seconds") if row.facts_as_of else None,
        "working_set": row.working_set or {},
        "working_version": row.working_version or 0,
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
    }


# ---------------------------------------------------------------- 工作集（确定性抽取）
def _add(bucket: dict, value: str, now: dt.datetime) -> None:
    value = (value or "").strip()
    if not value:
        return
    if len(bucket) >= 12 and value not in bucket:
        return
    item = bucket.get(value)
    if item is None:
        bucket[value] = {"first_seen": now.isoformat(timespec="seconds"),
                         "last_seen": now.isoformat(timespec="seconds"), "hits": 1}
    else:
        item["last_seen"] = now.isoformat(timespec="seconds")
        item["hits"] = int(item.get("hits", 1)) + 1


def extract_entities(text: str, now: dt.datetime | None = None) -> dict:
    """从一段文本里确定性地抽取实体（不调 LLM，结果可复现）。"""
    now = now or dt.datetime.now()
    text = text or ""
    found: dict[str, dict] = {}
    for m in SKU_RE.findall(text):
        _add(found.setdefault("skus", {}), m.upper(), now)
    for m in STYLE_RE.findall(text):
        _add(found.setdefault("styles", {}), m.upper(), now)
    sku_sizes = {s.split("-")[-1].upper() for s in SKU_RE.findall(text)}
    for m in SIZE_RE.findall(text):
        if m.upper() in sku_sizes:  # SKU 里的尺码已归入 skus，避免重复计数
            continue
        _add(found.setdefault("sizes", {}), m.upper(), now)
    for m in COLOR_RE.findall(text):
        _add(found.setdefault("colors", {}), m, now)
    for word in DEPARTMENTS:
        if word in text:
            _add(found.setdefault("categories", {}), word, now)
    for word in METRIC_WORDS:
        if word in text:
            _add(found.setdefault("metrics", {}), word, now)
    if (tm := TIME_RE.search(text)) is not None:
        found.setdefault("time_range", {})["label"] = tm.group(1).replace(" ", "")
        found["time_range"]["last_seen"] = now.isoformat(timespec="seconds")
    return found


def merge_working_set(prev: dict | None, new: dict, now: dt.datetime | None = None) -> dict:
    """并集合并（只追加不覆盖），并刷新 version / updated_at。"""
    now = now or dt.datetime.now()
    merged: dict = {}
    for key in ("skus", "styles", "sizes", "colors", "categories", "metrics"):
        bucket: dict = dict((prev or {}).get(key) or {})
        for value, meta in (new.get(key) or {}).items():
            if value in bucket:
                bucket[value] = {**bucket[value], "last_seen": meta.get("last_seen", ""),
                                 "hits": max(int(bucket[value].get("hits", 1)),
                                             int(meta.get("hits", 1)))}
            else:
                bucket[value] = meta
        if bucket:
            merged[key] = bucket
    if prev and prev.get("time_range"):
        merged["time_range"] = dict(prev["time_range"])
    if new.get("time_range"):
        merged["time_range"] = {**merged.get("time_range", {}), **new["time_range"]}
    merged["version"] = int((prev or {}).get("version", 0)) + 1
    merged["updated_at"] = now.isoformat(timespec="seconds")
    return merged


def working_set_text(working_set: dict | None) -> str:
    """把实体表渲染成紧凑文本（进系统提示）。"""
    ws = working_set or {}
    labels = {"skus": "SKU", "styles": "款号", "sizes": "尺码", "colors": "颜色",
              "categories": "品类", "metrics": "关注指标"}
    lines: list[str] = []
    for key, label in labels.items():
        bucket = ws.get(key) or {}
        if bucket:
            lines.append(f"{label}: " + ", ".join(list(bucket)[:8]))
    if (tr := ws.get("time_range") or {}).get("label"):
        lines.append(f"时间窗: {tr['label']}")
    if not lines:
        return ""
    meta = f"version={ws.get('version', 0)} updated_at={ws.get('updated_at', '')}"
    return f"<working-set {meta}>\n" + "\n".join(lines) + "\n</working-set>"


# ---------------------------------------------------------------- 事实卡（规则抽取）
def _split_sentences(text: str) -> list[str]:
    """按句号/分号/换行切句，丢掉 Markdown 表格行、标题行与代码块（事实卡只留结论句）。"""
    parts = re.split(r"(?<=[。；;!?])\s*|\n+", text or "")
    cleaned: list[str] = []
    for part in parts:
        line = part.strip(" ·-")
        if not line or line.startswith(("|", "#", "```", ">", "---")):
            continue
        if line.count("|") >= 2:  # 表格行残片
            continue
        cleaned.append(line.replace("**", "").strip())
    return cleaned


def _tool_summary_text(raw: str) -> str:
    """从工具结果 envelope 里取 `summary` 文本（取不到就退回原文）。

    复审 R5：早先事实卡只从 JSON 里正则抠 `key=数字`，抽出来是 `693，700` 这种**位置化数字**，
    模型读到 `[get_inventory] 693，700` 根本分不清哪个是可用、哪个是现存。
    现在优先用 envelope 自带的 `summary`（本来就是 `available=693；on_hand=700` 形式），
    渲染成 `available=693；on_hand=700`，可读且可核对。
    """
    match = _SUMMARY_FIELD_RE.search(raw or "")
    if not match:
        return raw or ""
    try:
        return json.loads(f'"{match.group(1)}"')
    except Exception:  # noqa: BLE001 —— 反转义失败就用原始片段
        return match.group(1)


def build_facts_card(messages: list[dict]) -> tuple[str, str]:
    """从历史消息生成事实卡：返回 (card_text, trigger)。

    messages 元素：{"role","content","tool_trace"}；tool_trace 里带 result_id 的工具结果会
    被登记为「取数事实」，assistant 回答里带数字的短句被登记为「结论」。
    """
    settings = get_settings()
    tool_facts: list[str] = []
    claim_facts: list[str] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            for trace in msg.get("tool_trace") or []:
                if trace.get("type") != "result":
                    continue
                raw = str(trace.get("result") or "")
                rid = re.search(r"res_[0-9a-f]{6,32}", raw)
                tool = trace.get("tool") or "tool"
                if "error" in raw and "无权限" in raw:
                    tool_facts.append(f"- [{tool}] 无权限（{rid.group(0) if rid else 'no-rid'}）")
                elif rid:
                    summary = _tool_summary_text(raw)
                    pairs = _SUMMARY_PAIR_RE.findall(summary)[:4]
                    detail = "；".join(f"{key}={value}" for key, value in pairs)
                    if not detail:
                        detail = summary.strip()[:80]
                    tool_facts.append(
                        f"- [{tool}] {detail or '取数成功'}（{rid.group(0)}）"
                    )
            for sentence in _split_sentences(str(msg.get("content") or "")):
                if len(sentence) > 100 or not re.search(r"\d", sentence):
                    continue
                claim_facts.append(f"- {sentence}")
        elif role == "user":
            continue
    seen: set[str] = set()
    facts: list[str] = []
    for item in claim_facts + tool_facts:
        if item in seen:
            continue
        seen.add(item)
        facts.append(item)
    # 复审 T2：事实卡正文来自"模型自己的结论句 + 工具摘要"，可能夹带用户原文。进标签前把尖括号
    # 转义，否则一句 `</analysis-facts>` 就能提前结束卡片，把后面的文字甩到卡片外面去。
    body = "\n".join(escape_tag_text(item) for item in facts[-24:])
    header = (f'{_CTX_OPEN} version="auto" as_of="{dt.datetime.now().isoformat(timespec="seconds")}" '
              f'rule="deterministic">')
    card = f"{header}\n{body}\n</analysis-facts>" if body else ""
    if card and len(card) > settings.memory_summary_chars * 2:
        card = card[: settings.memory_summary_chars * 2] + "\n</analysis-facts>"
    trigger = "rules"
    return card, trigger


def _history_rows(session, session_id: str) -> list[dict]:
    rows = session.scalars(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
    ).all()
    return [{"role": r.role, "content": r.content or "", "tool_trace": r.tool_trace or []}
            for r in rows]


def _card_hash(card: str) -> str:
    return hashlib.sha1((card or "").encode("utf-8")).hexdigest()[:16]


def refresh_memory(session, session_id: str, message: str) -> dict:
    """每轮对话后调用：追加工作集实体、必要时重建事实卡。"""
    settings = get_settings()
    now = dt.datetime.now()
    row = ensure_memory(session, session_id)
    history = _history_rows(session, session_id)
    history_text = "".join(m["content"] for m in history)
    msg_count = len(history)
    tokens = estimate_tokens(history_text)

    row.working_set = merge_working_set(row.working_set or {}, extract_entities(message, now), now)
    row.working_version = int(row.working_set.get("version", 0))

    need_card = bool(history) and (
        msg_count > settings.memory_facts_trigger_messages
        or tokens > settings.memory_facts_trigger_tokens
        or not row.facts_card
    )
    if need_card:
        card, trigger = build_facts_card(history)
        new_hash = _card_hash(card)
        if card and new_hash != row.facts_hash:
            row.facts_card = card
            row.facts_hash = new_hash
            row.facts_version = int(row.facts_version or 0) + 1
            row.facts_as_of = now
            row.facts_trigger = (f"messages>{settings.memory_facts_trigger_messages}"
                                 if msg_count > settings.memory_facts_trigger_messages
                                 else f"tokens>{settings.memory_facts_trigger_tokens}"
                                 if tokens > settings.memory_facts_trigger_tokens else trigger)
    row.updated_at = now
    session.flush()
    return {"facts_version": row.facts_version, "working_version": row.working_version,
            "facts_trigger": row.facts_trigger}


# ---------------------------------------------------------------- 组装上下文
def build_memory_context(session, session_id: str) -> dict:
    """生成注入系统提示的记忆上下文（事实卡 + 工作集，带字符上限）。"""
    settings = get_settings()
    row = session.get(ChatMemory, session_id)
    facts = (row.facts_card if row else "") or ""
    working = working_set_text(row.working_set if row else {})
    parts = [p for p in (facts, working) if p]
    text = "\n\n".join(parts)
    if len(text) > settings.memory_context_chars:
        text = text[: settings.memory_context_chars] + "…"
    return {
        "text": text,
        "facts_version": int(row.facts_version) if row else 0,
        "working_version": int(row.working_version) if row else 0,
    }


# ---------------------------------------------------------------- query_plan 落库 / 追问去重
def _clarify_key(plan: dict) -> str:
    missing = ((plan.get("clarify") or {}).get("missing") or [])
    return ",".join(sorted(missing))


def should_ask_clarify(session, session_id: str, plan: dict) -> bool:
    """同一缺口只追问一次：本会话已就同一组缺失项追问过就不再问。"""
    if not plan.get("needs_clarify"):
        return False
    key = _clarify_key(plan)
    if not key:
        return False
    rows = session.scalars(
        select(ChatQueryPlan).where(ChatQueryPlan.session_id == session_id)
    ).all()
    for row in rows:
        if not row.asked_clarify:
            continue
        if _clarify_key(row.payload or {}) == key:
            return False
    return True


def save_query_plan(session, *, session_id: str, username: str, department: str | None,
                    plan: dict, message_id: int = 0, asked_clarify: bool = False) -> None:
    session.add(ChatQueryPlan(
        session_id=session_id, message_id=message_id, username=username or "",
        department=department, plan_id=plan.get("plan_id", ""),
        intent=plan.get("intent", ""), confidence=float(plan.get("confidence") or 0.0),
        asked_clarify=asked_clarify, payload=plan, created_at=dt.datetime.now(),
    ))


def latest_query_plan(session, session_id: str) -> dict | None:
    row = session.scalars(
        select(ChatQueryPlan).where(ChatQueryPlan.session_id == session_id)
        .order_by(ChatQueryPlan.id.desc())
    ).first()
    return (row.payload or None) if row else None
