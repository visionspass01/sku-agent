"""用户偏好画像（L1 长期记忆的最小实现）。

边界：只存"这个人怎么用"——输出偏好、口径偏好、关注 SKU 清单；**不存**对话原文
（那是 `chat_message`）、**不存**结论档案（那是 L3 `analysis_result`）。

用途：每轮对话注入系统提示（≤200 字），让"默认看近 7 天""口径偏保守"这类偏好
跨会话生效；同时给运营一个"关注清单"，巡检/对话都能按它优先处理。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.db.models import UserProfile

MAX_PROFILE_CHARS = 200


def get_profile(session: Session, username: str) -> dict:
    """读画像；没有就返回空默认值（不写库，避免"读"产生副作用）。"""
    row = session.get(UserProfile, username)
    if row is None:
        return {"username": username, "department": "", "prefs": {}, "watch_skus": [],
                "updated_at": None}
    return {
        "username": row.username,
        "department": row.department or "",
        "prefs": dict(row.prefs or {}),
        "watch_skus": list(row.watch_skus or []),
        "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
    }


def upsert_profile(session: Session, username: str, department: str = "",
                   prefs: dict | None = None, watch_skus: list | None = None) -> dict:
    """写画像（整体覆盖 prefs/watch_skus；None 表示保留原值）。"""
    row = session.get(UserProfile, username)
    if row is None:
        row = UserProfile(username=username, prefs={}, watch_skus=[])
        session.add(row)
    if department:
        row.department = department
    if prefs is not None:
        row.prefs = dict(prefs)
    if watch_skus is not None:
        row.watch_skus = [str(s) for s in watch_skus][:20]
    row.updated_at = dt.datetime.now()
    session.flush()
    return get_profile(session, username)


def profile_text(session: Session, username: str, max_chars: int = MAX_PROFILE_CHARS) -> str:
    """渲染成系统提示片段（≤max_chars，长值截断，避免挤占上下文）。"""
    data = get_profile(session, username)
    prefs = data.get("prefs") or {}
    watch = data.get("watch_skus") or []
    lines: list[str] = []
    for key, label in (("output_style", "输出偏好"), ("metric_pref", "口径偏好"),
                       ("time_window", "默认时间窗"), ("note", "其他")):
        value = prefs.get(key)
        if value:
            lines.append(f"- {label}：{str(value)[:40]}")
    if watch:
        lines.append(f"- 关注 SKU（优先处理）：{', '.join(watch[:8])}")
    if not lines:
        return ""
    text = "## 用户偏好（长期画像，跨会话有效；与用户当轮要求冲突时以当轮为准）\n" + "\n".join(lines)
    return text[:max_chars]


def parse_prefs(raw: dict | None) -> dict:
    """只接受白名单字段，避免把任意 JSON 塞进提示。"""
    allowed = ("output_style", "metric_pref", "time_window", "note")
    out: dict = {}
    for key in allowed:
        value = (raw or {}).get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:80]
    return out
