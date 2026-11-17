"""分析档案主文档的「结构化合并」：渲染永远由代码做，LLM 只负责结论叙述。

为什么需要这个模块（评审缺陷 A1/A2，静默覆盖）：
`data/analyses/*.md` 早先的文件名是 `<分钟>-<标题 40 字>.md`，标题又由「问题前 16 字 +
结论前 20 字」拼成 —— 同一分钟、同一问题、证据不同的两次分析会落到**同一个文件**，
后写的直接覆盖前一份正文：DB 里两行、磁盘上一份，`get_analysis(A)` 返回
「A 的字段 + B 的正文」（front-matter 的 analysis_id 还是 B），而且没有任何日志。

现在的机制（并发写安全）：
1. 路径按「部门 + 问题」派生（topic key）→ 同一问题只有一份**主文档**，不同问题天然不同文件；
2. 写入是 CAS：「读时取哈希 → 短锁内校验哈希 → 原子替换」，**长耗时的合并/模型调用不在锁内**；
3. 哈希不一致（有人在我们读之后写过）→ **保留三份文档**：原文档快照、新文档快照、合并后的主文档；
4. 结构合并（entries 并集 / 指标 / 证据）由代码完成；**LLM 只能改写「结论摘要」一段**，
   且必须保留原文全部数字与 result_id，否则整段被拒并回退到确定性合并；
5. 「各次分析结论」一节永远按原文渲染 → LLM 不可能改掉原始记录；
6. 合并失败 / LLM 不可用 / CAS 反复冲突 → 落 pending 队列，主文档保持原样，**绝不覆盖**。

事实源：`analysis_result` 行（问题/结论/指标/证据都是列）才是事实源，markdown 只是投影。
因此合并坏了可以 `scripts/render_analysis_md.py` 从 DB 重渲染，取证材料不会丢。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from app.config import get_settings

logger = logging.getLogger("sku-agent.analysis_merge")

MERGE_BEGIN = "<!-- analysis-entries:begin -->"
MERGE_END = "<!-- analysis-entries:end -->"
_JSON_BLOCK = re.compile(re.escape(MERGE_BEGIN) + r"\s*```json\s*(.*?)\s*```\s*" + re.escape(MERGE_END),
                         re.S)
_LOG_LOCK = threading.Lock()
_LOG_NAME = "_merge_log.jsonl"
_PENDING_DIR = "_pending"


class MergeRejected(RuntimeError):
    """合并结果没通过校验（缺 analysis_id / 缺 evidence / 数字被改写）。"""


# ---------------------------------------------------------------- 数据结构
@dataclass
class Entry:
    """一次分析的贡献（合并的最小单位）。"""

    analysis_id: str
    as_of: str = ""
    title: str = ""
    question: str = ""
    conclusion: str = ""
    key_metrics: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    plan_id: str = ""
    metric_version: str = ""

    def to_dict(self) -> dict:
        return {
            "analysis_id": self.analysis_id, "as_of": self.as_of, "title": self.title,
            "question": self.question, "conclusion": self.conclusion,
            "key_metrics": dict(self.key_metrics or {}),
            "evidence": [str(e) for e in (self.evidence or [])],
            "plan_id": self.plan_id, "metric_version": self.metric_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Entry:
        return cls(
            analysis_id=str(data.get("analysis_id") or ""),
            as_of=str(data.get("as_of") or ""),
            title=str(data.get("title") or ""),
            question=str(data.get("question") or ""),
            conclusion=str(data.get("conclusion") or ""),
            key_metrics=dict(data.get("key_metrics") or {}),
            evidence=[str(e) for e in (data.get("evidence") or [])],
            plan_id=str(data.get("plan_id") or ""),
            metric_version=str(data.get("metric_version") or ""),
        )

    @classmethod
    def from_row(cls, row) -> Entry:  # noqa: ANN001 —— AnalysisResult ORM 行
        return cls(
            analysis_id=str(row.analysis_id),
            as_of=row.created_at.isoformat(timespec="seconds") if row.created_at else "",
            title=str(row.title or ""),
            question=str(row.question or ""),
            conclusion=str(row.conclusion or ""),
            key_metrics=dict(row.key_metrics or {}),
            evidence=[str(e) for e in (row.evidence or [])],
            plan_id=str(row.plan_id or ""),
            metric_version=str(row.metric_version or ""),
        )


@dataclass
class Doc:
    """一份主文档：topic 固定，entries 随分析次数增长。"""

    topic: str
    department: str
    title: str
    entries: list[Entry] = field(default_factory=list)
    merged_summary: str = ""
    merge_count: int = 0
    updated_at: str = ""
    metric_version: str = ""

    def analysis_ids(self) -> list[str]:
        return [e.analysis_id for e in self.entries]

    def evidence_ids(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries:
            for rid in entry.evidence:
                if rid not in seen:
                    seen.append(rid)
        return seen


# ---------------------------------------------------------------- 路径 / 快照 / 日志
def _analyses_root() -> Path:
    return get_settings().analyses_path


def merge_log_path() -> Path:
    return _analyses_root() / _LOG_NAME


def pending_dir() -> Path:
    return _analyses_root() / _PENDING_DIR


def snapshot(canonical: Path, text: str, *, kind: str, analysis_id: str,
             at: dt.datetime) -> Path:
    """把某一份内容固化成**不可变**快照（kind = prev / new / merged-rejected）。

    冲突时至少要保留「原文档」与「新文档」两份，主文档才允许被合并结果替换。
    """
    stamp = f"{at:%Y%m%d-%H%M%S}"
    suffix = (analysis_id or "unknown")[:8]
    path = canonical.with_name(f"{canonical.stem}.{kind}-{stamp}-{suffix}.md")
    atomic_write(path, text)
    return path


def atomic_write(path: Path, text: str) -> None:
    """同目录临时文件 + fsync + os.replace：任何时刻读到的都是完整文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def file_digest(path: Path) -> str | None:
    """文件内容哈希；文件不存在返回 None（=「还没有人写过」）。"""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, NotADirectoryError):
        return None


def digest_text(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def log_event(event: dict) -> None:
    """追加结构化日志（一行一事件，便于 grep / 事后审计）。"""
    record = {"at": dt.datetime.now().isoformat(timespec="seconds"), **event}
    path = merge_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _LOG_LOCK, open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 —— 日志失败不能影响主流程
        logger.warning("写合并日志失败：%s", exc)
    if record.get("outcome") in ("conflict", "pending", "keep_both", "merge_rejected"):
        logger.warning("分析档案合并事件：%s", json.dumps(record, ensure_ascii=False, default=str)[:500])


def read_log(limit: int = 50) -> list[dict]:
    """最近 N 条合并日志（脚本/排障用）。"""
    path = merge_log_path()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    out: list[dict] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------- pending 队列
def enqueue_pending(payload: dict) -> Path:
    """合并未完成时把「待合并」落盘：主文档保持原样，等后续（工具层/离线脚本）处理。"""
    folder = pending_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{payload.get('topic', 'topic')}-{uuid.uuid4().hex[:8]}.json"
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=1))
    return path


def list_pending(limit: int | None = None) -> list[Path]:
    folder = pending_dir()
    if not folder.exists():
        return []
    paths = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
    return paths[:limit] if limit else paths


def load_pending(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def drop_pending(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 渲染 / 解析
def _fmt_metrics(entries: list[Entry]) -> list[str]:
    """指标合并：同 key 多值 → 并列并标注来源与冲突。"""
    values: dict[str, list[tuple[str, object]]] = {}
    order: list[str] = []
    for entry in entries:
        for key, value in (entry.key_metrics or {}).items():
            if key not in values:
                values[key] = []
                order.append(key)
            values[key].append((entry.analysis_id[:8], value))
    lines: list[str] = []
    for key in order:
        pairs = values[key]
        distinct = {json.dumps(v, ensure_ascii=False, default=str) for _, v in pairs}
        if len(distinct) == 1:
            lines.append(f"- {key}: {pairs[0][1]}（来源 `{pairs[0][0]}`）")
        else:
            joined = " / ".join(f"{value}（来源 `{aid}`）" for aid, value in pairs)
            lines.append(f"- {key}: {joined} ⚠ 口径冲突，需人工确认")
    return lines or ["- （无）"]


def _fmt_block(text: str, indent: str = "  ") -> str:
    body = (text or "").strip() or "（无）"
    return "\n".join(indent + line if line.strip() else line for line in body.splitlines())


def render_doc(doc: Doc) -> str:
    """渲染主文档：人读的部分 + 机器可读的 entries 块（下一次合并据此解析）。"""
    latest = doc.entries[-1].as_of if doc.entries else ""
    front = [
        "---",
        f"title: {doc.title}",
        f"topic: {doc.topic}",
        f"department: {doc.department}",
        f"analysis_ids: {', '.join(doc.analysis_ids())}",
        f"evidence: {', '.join(doc.evidence_ids())}",
        f"metric_version: {doc.metric_version}",
        f"merge_count: {doc.merge_count}",
        f"as_of: {latest}",
        f"updated_at: {doc.updated_at}",
        "---",
        "",
        f"# {doc.title}",
        "",
        "> 同一问题的多次分析会合并到这一份主文档；各次结论原文见「各次分析结论」，",
        "> 合并只整合叙述，不改写任何数字与 result_id。",
        "",
        "## 结论摘要（合并）",
        (doc.merged_summary or "（首次写入，暂无摘要）").strip(),
        "",
        "## 各次分析结论（原文，不改写）",
    ]
    if doc.entries:
        for entry in doc.entries:
            head = f"- **{entry.as_of or '未知时间'}**（`{entry.analysis_id}`）"
            front.append(head)
            front.append(_fmt_block(entry.conclusion))
    else:
        front.append("- （无）")
    front += ["", "## 关键指标"]
    front += _fmt_metrics(doc.entries)
    front += ["", "## 问题（背景）"]
    if doc.entries:
        for entry in doc.entries:
            front.append(f"- （`{entry.analysis_id[:8]}`）{entry.question or '（无）'}")
    else:
        front.append("- （无）")
    front += ["", "## 证据（result_id）"]
    evidence = doc.evidence_ids()
    front += [f"- {rid}" for rid in evidence] or ["- （无）"]
    front += [
        "",
        MERGE_BEGIN,
        "```json",
        json.dumps([e.to_dict() for e in doc.entries], ensure_ascii=False, indent=1),
        "```",
        MERGE_END,
        "",
    ]
    return "\n".join(front)


def _parse_front(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out: dict = {}
    for line in text[3:end].strip().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def parse_doc(text: str | None) -> Doc | None:
    """解析主文档；老格式（没有 entries 块）也能降级解析出单条 entry。"""
    if not text:
        return None
    front = _parse_front(text)
    match = _JSON_BLOCK.search(text)
    entries: list[Entry] = []
    if match:
        try:
            entries = [Entry.from_dict(item) for item in json.loads(match.group(1)) if item]
        except (json.JSONDecodeError, TypeError, AttributeError):
            entries = []
    if not entries:
        # 老文档 / 被人工编辑过：从 front-matter 与「## 结论」段落尽力还原一条 entry
        analysis_id = front.get("analysis_id") or ""
        conclusion = ""
        section = re.search(r"##\s*结论\s*\n(.*?)(?:\n##\s|\Z)", text, re.S)
        if section:
            conclusion = section.group(1).strip()
        question = ""
        q_section = re.search(r"##\s*背景\s*\n(.*?)(?:\n##\s|\Z)", text, re.S)
        if q_section:
            question = q_section.group(1).strip()
        if analysis_id or conclusion:
            entries = [Entry(
                analysis_id=analysis_id or "legacy",
                as_of=front.get("as_of", ""),
                title=front.get("title", ""),
                question=question if question != "（无）" else "",
                conclusion=conclusion if conclusion != "（无）" else "",
                evidence=[e.strip() for e in (front.get("result_ids") or "").split(",") if e.strip()],
                metric_version=front.get("metric_version", ""),
            )]
    try:
        merge_count = int(front.get("merge_count") or 0)
    except ValueError:
        merge_count = 0
    return Doc(
        topic=front.get("topic", ""),
        department=front.get("department", ""),
        title=front.get("title", "") or (entries[0].title if entries else ""),
        entries=entries,
        merged_summary=_extract_summary(text),
        merge_count=merge_count,
        updated_at=front.get("updated_at", ""),
        metric_version=front.get("metric_version", ""),
    )


def _extract_summary(text: str) -> str:
    section = re.search(r"##\s*结论摘要[^\n]*\n(.*?)(?:\n##\s|\Z)", text, re.S)
    if not section:
        return ""
    body = section.group(1).strip()
    return "" if body.startswith("（首次写入") else body


# ---------------------------------------------------------------- 合并
def merge_entry(doc: Doc, incoming: Entry) -> Doc:
    """把一次分析并进主文档（按 analysis_id 去重：同 id 覆盖，不同 id 追加）。"""
    entries = [e for e in doc.entries if e.analysis_id != incoming.analysis_id]
    entries.append(incoming)
    entries.sort(key=lambda e: (e.as_of or "", e.analysis_id))
    out = Doc(
        topic=doc.topic, department=doc.department, title=doc.title or incoming.title,
        entries=entries, merged_summary=doc.merged_summary, merge_count=doc.merge_count,
        updated_at=doc.updated_at, metric_version=incoming.metric_version or doc.metric_version,
    )
    return out


def metric_conflicts(doc: Doc) -> list[dict]:
    values: dict[str, list[tuple[str, object]]] = {}
    for entry in doc.entries:
        for key, value in (entry.key_metrics or {}).items():
            values.setdefault(key, []).append((entry.analysis_id[:8], value))
    conflicts: list[dict] = []
    for key, pairs in values.items():
        distinct = {json.dumps(v, ensure_ascii=False, default=str) for _, v in pairs}
        if len(distinct) > 1:
            conflicts.append({"metric": key,
                              "values": [{"analysis_id": aid, "value": value} for aid, value in pairs]})
    return conflicts


def deterministic_summary(doc: Doc) -> str:
    """确定性摘要（不调模型）：按时间列出各次结论的第一句 + 来源。"""
    lines: list[str] = []
    for entry in doc.entries:
        first = next((ln.strip() for ln in (entry.conclusion or "").splitlines() if ln.strip()), "")
        lines.append(f"- （`{entry.analysis_id[:8]}`，{entry.as_of or '未知时间'}）{first[:160]}")
    return "\n".join(lines) if lines else ""


def _numeric_tokens(text: str) -> set[str]:
    from app.agents.grounding import CLAIM_RE, RES_ID_RE

    tokens = {m.group(0).replace(" ", "") for m in CLAIM_RE.finditer(text or "")}
    tokens |= set(RES_ID_RE.findall(text or ""))
    return tokens


def preserves_evidence(summary: str, entries: list[Entry]) -> bool:
    """LLM 摘要必须是「忠实整合」：原文所有「数字+单位」与 result_id 都得保留。"""
    required: set[str] = set()
    for entry in entries:
        required |= _numeric_tokens(entry.conclusion)
        required |= {str(e) for e in entry.evidence}
    if not required:
        return True
    return required <= _numeric_tokens(summary)


RECONCILE_SYSTEM = (
    "你是电商库存分析档案的编辑，只做一件事：把同一问题下的多次分析结论整合成一段摘要。\n"
    "硬规则：\n"
    "1. 只能使用给出的结论里出现过的数字与 result_id，不得新增、不得改写、不得省略；\n"
    "2. 数字后面保留原有的 result_id 标注；口径冲突就并列写出并注明「口径冲突」；\n"
    "3. 不做新的推断，不改变结论方向；\n"
    "4. 输出 JSON：{\"summary\": \"<Markdown 摘要，可用列表>\", \"conflicts\": [\"<冲突说明>\"]}"
)


class MergeOutput(BaseModel):
    """LLM 合并的输出契约（结构由代码渲染，模型只交这一段文字）。"""

    summary: str = ""
    conflicts: list[str] = []


async def reconcile_summary(doc: Doc, *, use_llm: bool = True) -> tuple[str, dict]:
    """生成「结论摘要」：优先让模型整合，失败/不忠实则回退到确定性摘要。

    为什么是 async：`LLMClient.invoke_json` 是协程。调用方必须来自异步上下文
    （工具层 / 应用启动 / 离线脚本里的 `asyncio.run`），**绝不能在同步写盘路径里调用**——
    否则要么阻塞事件循环，要么在已有循环里 `asyncio.run` 直接报错。
    """
    if len(doc.entries) < 2:
        single = doc.entries[0] if doc.entries else None
        return (single.conclusion if single else ""), {"mode": "single"}
    conflicts = metric_conflicts(doc)
    if not use_llm:
        return deterministic_summary(doc), {"mode": "deterministic", "conflicts": conflicts}
    try:
        from app.llm import get_llm

        payload = {
            "topic": doc.title,
            "conflicts": conflicts,
            "entries": [
                {"analysis_id": e.analysis_id, "as_of": e.as_of,
                 "conclusion": e.conclusion, "key_metrics": e.key_metrics,
                 "evidence": e.evidence}
                for e in doc.entries
            ],
        }
        result = await get_llm().invoke_json(
            RECONCILE_SYSTEM, json.dumps(payload, ensure_ascii=False, indent=1),
            MergeOutput, max_tokens=900,
        )
        summary = (result.summary or "").strip()
        if summary and preserves_evidence(summary, doc.entries):
            return summary, {"mode": "llm", "conflicts": result.conflicts}
        return deterministic_summary(doc), {
            "mode": "deterministic", "conflicts": conflicts,
            "reason": "llm_summary_not_faithful" if summary else "llm_summary_empty",
        }
    except Exception as exc:  # noqa: BLE001 —— 模型不可用不能让档案写不进去
        return deterministic_summary(doc), {
            "mode": "deterministic", "conflicts": conflicts,
            "reason": f"{type(exc).__name__}: {exc}"[:160],
        }


def validate_render(rendered: str, doc: Doc) -> None:
    """写回前的最后一道校验：主干内容不能少（缺就是合并出错，宁可拒绝写回）。"""
    missing_ids = [aid for aid in doc.analysis_ids() if aid not in rendered]
    missing_evidence = [rid for rid in doc.evidence_ids() if rid not in rendered]
    if missing_ids or missing_evidence:
        raise MergeRejected(
            f"合并结果缺少 analysis_id={missing_ids} / evidence={missing_evidence}"
        )
    if MERGE_BEGIN not in rendered or MERGE_END not in rendered:
        raise MergeRejected("合并结果缺少机器可读的 entries 块")
