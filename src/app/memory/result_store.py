"""L0 工作记忆：工具结果契约。

约定（与设计文档 1.5 一致）：
1. 默认聚合优先、明细行数受限：一次工具返回超过 `memory_max_inline_rows`（默认 50）行时，
   只回 `summary` + `artifact_path`，原始明细落 `data/artifacts/<session_id>/<result_id>.jsonl`；
2. 每条工具结果都带 `result_id` 与 `as_of`，回答必须引用 result_id，便于复核；
3. 结果登记到 `tool_result` 表，形成可回溯的「取数台账」（谁、何时、用什么工具、多少行）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import threading
import uuid
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db.engine import session_scope
from app.db.models import ToolResult

_SEQ = itertools.count(1)
_LOCK = threading.Lock()

# 摘要里优先展示的标量字段（中台返回的业务关键指标）
_PRIORITY_KEYS = (
    "sku_id", "style_id", "category", "department", "available", "on_hand", "locked",
    "reserved_crm", "order_hold", "in_transit_qty", "in_transit_7d", "daily_avg_7d",
    "sell_through_days", "stockout_risk", "safety_stock", "reorder_point", "sales",
    "sales_rate", "days", "qty", "amount", "status", "approval_id", "po_no", "po_list",
    "cached", "sales_30d", "daily_avg_30d",
)
_MAX_SCALARS = 14


def new_result_id(session_id: str | None = None) -> str:
    """生成稳定前缀 + 唯一后缀的 result_id（同进程内单调递增，便于排序）。"""
    with _LOCK:
        seq = next(_SEQ)
    raw = f"{session_id or 'nosession'}|{dt.datetime.now().isoformat()}|{seq}|{uuid.uuid4().hex[:8]}"
    return "res_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)] + f"…（已截断 {len(text) - limit} 字）"


def _largest_list(payload: dict) -> tuple[str | None, list]:
    """找出结果里最长的列表字段（中台列表接口常叫 items / rows / orders ...）。"""
    best_key, best_len = None, 0
    for key, value in payload.items():
        if isinstance(value, list) and len(value) > best_len:
            best_key, best_len = key, len(value)
    return best_key, (payload.get(best_key) if best_key else [])


def summarize(tool: str, payload: dict, *, row_count: int = 0, truncated: bool = False,
              max_rows: int = 50) -> str:
    """确定性摘要（不调用 LLM）：优先标量指标 + 行数说明，保证可复现。"""
    error = payload.get("error")
    if error:
        return clip(f"{tool} 调用失败：{error}", get_settings().memory_summary_chars)
    parts: list[str] = []
    for key in _PRIORITY_KEYS:
        value = payload.get(key)
        if value is None or value == "" or isinstance(value, (list, dict)):
            continue
        parts.append(f"{key}={value}")
        if len(parts) >= _MAX_SCALARS:
            break
    if not parts:  # 兜底：取前若干个标量
        for key, value in payload.items():
            if isinstance(value, (list, dict)) or value is None or value == "":
                continue
            parts.append(f"{key}={value}")
            if len(parts) >= 8:
                break
    text = f"{tool}：" + ("；".join(parts) if parts else "（无可展示标量）")
    if truncated:
        text += f"；明细 {row_count} 行超过 {max_rows} 行内联上限，已转存 artifact_path，请用聚合值与 result_id 作答"
    elif row_count:
        text += f"；明细共 {row_count} 行"
    return clip(text, get_settings().memory_summary_chars)


def artifact_path_for(result_id: str, session_id: str | None) -> str:
    settings = get_settings()
    folder = settings.artifacts_path / (session_id or "shared")
    return str(folder / f"{result_id}.jsonl")


def write_artifact(result_id: str, session_id: str | None, tool: str, rows: list) -> str:
    """把超限明细落盘为 jsonl（一行一条，附 result_id/tool 头信息，便于复核）。"""
    path = artifact_path_for(result_id, session_id)
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_meta": {"result_id": result_id, "tool": tool,
                                      "row_count": len(rows), "as_of": now_iso()}},
                            ensure_ascii=False) + "\n")
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return path


def record_result(*, result_id: str, session_id: str | None, username: str, tool: str,
                  as_of: str, summary: str, row_count: int, truncated: bool,
                  artifact_path: str = "", pruned: bool = False, meta: dict | None = None) -> None:
    """登记到 tool_result 表（取数台账）。失败不影响对话主流程。"""
    try:
        with session_scope() as session:
            session.add(ToolResult(
                result_id=result_id, session_id=session_id or "", username=username or "",
                tool=tool, as_of=as_of, summary=summary, row_count=row_count,
                truncated=truncated, artifact_path=artifact_path, pruned=pruned,
                meta=meta or {}, created_at=dt.datetime.now(),
            ))
    except Exception:  # noqa: BLE001 —— 台账失败不能让取数失败
        pass


def finalize(tool: str, data: Any, *, session_id: str | None = None, username: str = "",
             max_rows: int | None = None) -> dict:
    """把任意工具返回值包成 L0 契约：原字段全保留 + result_id/as_of/summary/truncated。

    保留原始字段是为了兼容既有调用方（前端 / 测试 / LLM），只做「追加」不做「重命名」。
    """
    settings = get_settings()
    max_rows = max_rows or settings.memory_max_inline_rows
    payload: dict = dict(data) if isinstance(data, dict) else {"data": data}
    data_as_of = payload.get("as_of")
    queried_at = now_iso()
    row_key, rows = _largest_list(payload)
    row_count = len(rows)
    truncated = row_count > max_rows
    result_id = new_result_id(session_id)
    artifact_path = ""
    if truncated and row_key:
        artifact_path = write_artifact(result_id, session_id, tool, rows)
        payload[row_key] = rows[:3]
        payload["preview_rows"] = 3
        payload["row_count"] = row_count
        payload["truncated"] = True
        payload["artifact_path"] = artifact_path
    else:
        payload["truncated"] = False
        if row_count:
            payload["row_count"] = row_count
    payload["summary"] = summarize(tool, payload, row_count=row_count, truncated=truncated,
                                   max_rows=max_rows)
    payload["result_id"] = result_id
    payload["as_of"] = str(data_as_of) if data_as_of else queried_at
    payload["queried_at"] = queried_at
    payload["tool"] = tool
    record_result(result_id=result_id, session_id=session_id, username=username, tool=tool,
                  as_of=str(payload["as_of"]), summary=payload["summary"], row_count=row_count,
                  truncated=truncated, artifact_path=artifact_path,
                  meta={"error": payload.get("error", "")} if payload.get("error") else {})
    return payload


def get_result(result_id: str) -> dict | None:
    with session_scope() as session:
        row = session.scalars(select(ToolResult).where(ToolResult.result_id == result_id)).first()
        if row is None:
            return None
        return {"result_id": row.result_id, "tool": row.tool, "as_of": row.as_of,
                "summary": row.summary, "row_count": row.row_count,
                "truncated": row.truncated, "artifact_path": row.artifact_path,
                "session_id": row.session_id, "username": row.username}


def list_results(session_id: str, limit: int = 20) -> list[dict]:
    with session_scope() as session:
        rows = session.scalars(
            select(ToolResult).where(ToolResult.session_id == session_id)
            .order_by(ToolResult.id.desc()).limit(limit)
        ).all()
        return [{"result_id": r.result_id, "tool": r.tool, "as_of": r.as_of,
                 "summary": r.summary, "truncated": r.truncated,
                 "artifact_path": r.artifact_path,
                 "created_at": r.created_at.isoformat(timespec="seconds")} for r in rows]


def cited_result_ids(text: str) -> list[str]:
    """从回答文本里抽出引用到的 result_id（用于前端展示 / 测试校验）。"""
    import re

    seen: list[str] = []
    for match in re.findall(r"res_[0-9a-f]{6,32}", text or ""):
        if match not in seen:
            seen.append(match)
    return seen
