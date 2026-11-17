"""档案主文档「摘要整合」的可靠性指标（评审 2.2-8）。

问题：主文档合并是 fire-and-forget（`_spawn_background`），成功与否只写进
`data/analyses/_merge_log.jsonl`。进程被 kill、模型超时、CAS 反复冲突时：
- 没有任何指标告诉运维"积压了多少、卡了多久"；
- 也没人回答得了"合并没有丢"这句话——只能靠人去 tail 日志。

这里给出**进程内**最小可观测面 + 明确的 SLO 判定：
- 计数：attempts / ok / failed / abandoned；
- 积压：`pending` 条数与**最老一条的年龄**；
- 降级：`pending > 0 且最老年龄 > MERGE_DEGRADE_MINUTES` → `degraded=True`。

为什么放在内存而不落库：合并本身是"投影维护"，真相在 DB 行与 md 文件；
指标重启清零是可接受的（积压量每次 drain 都会重新探测）。
"""

from __future__ import annotations

import threading
import time

MERGE_DEGRADE_MINUTES = 10.0

_lock = threading.Lock()
_stats = {
    "attempts": 0,
    "ok": 0,
    "failed": 0,
    "abandoned": 0,
    "stale": 0,
    "last_error": "",
    "last_ok_at": 0.0,
    "last_drain_at": 0.0,
    "durations_ms": [],
}


def reset() -> None:
    with _lock:
        for key in ("attempts", "ok", "failed", "abandoned", "stale"):
            _stats[key] = 0
        _stats["last_error"] = ""
        _stats["last_ok_at"] = 0.0
        _stats["last_drain_at"] = 0.0
        _stats["durations_ms"] = []


def record(outcome: str, *, elapsed_ms: float = 0.0, error: str = "") -> None:
    """登记一次合并结果。`outcome`：ok / failed / abandoned / stale / drain。"""
    with _lock:
        if outcome == "drain":
            _stats["last_drain_at"] = time.time()
            return
        _stats["attempts"] += 1
        if outcome == "ok":
            _stats["ok"] += 1
            _stats["last_ok_at"] = time.time()
            if elapsed_ms:
                durations = _stats["durations_ms"]
                durations.append(round(float(elapsed_ms), 1))
                del durations[:-200]
        elif outcome == "abandoned":
            _stats["abandoned"] += 1
        elif outcome == "stale":
            _stats["stale"] += 1
        else:
            _stats["failed"] += 1
        if error:
            _stats["last_error"] = error[:200]


def snapshot(pending: list[dict] | None = None) -> dict:
    """指标快照。`pending` 传入待合并队列（来自 analysis_merge.list_pending）以算积压。"""
    with _lock:
        data = {k: (list(v) if isinstance(v, list) else v) for k, v in _stats.items()}
    durations = sorted(data.pop("durations_ms") or [])
    pending_count = len(pending or [])
    oldest_age_min = 0.0
    if pending:
        now = time.time()
        ages = []
        for item in pending:
            stamp = item.get("enqueued_at") or item.get("at") or ""
            try:
                import datetime as dt

                ages.append(now - dt.datetime.fromisoformat(str(stamp)).timestamp())
            except Exception:  # noqa: BLE001 —— 时间戳解析失败就当"刚刚入队"
                continue
        oldest_age_min = round(max(ages) / 60.0, 1) if ages else 0.0
    degraded = pending_count > 0 and oldest_age_min >= MERGE_DEGRADE_MINUTES
    return {
        "attempts": data["attempts"],
        "ok": data["ok"],
        "failed": data["failed"],
        "abandoned": data["abandoned"],
        "stale": data["stale"],
        "success_rate": round(data["ok"] / data["attempts"], 4) if data["attempts"] else 0.0,
        "p95_ms": _percentile(durations, 0.95),
        "pending": pending_count,
        "oldest_pending_minutes": oldest_age_min,
        "degraded": degraded,
        "slo": f"待合并积压超过 {MERGE_DEGRADE_MINUTES:g} 分钟即视为降级",
        "last_error": data["last_error"],
        "last_ok_at": _iso(data["last_ok_at"]),
        "last_drain_at": _iso(data["last_drain_at"]),
        "note": "进程内指标（重启清零）；合并失败的重试由调度器 drain 承担",
    }


def _percentile(ordered: list[float], ratio: float) -> float:
    if not ordered:
        return 0.0
    import math

    idx = max(0, math.ceil(ratio * len(ordered)) - 1)
    return float(ordered[idx])


def _iso(stamp: float) -> str:
    if not stamp:
        return ""
    import datetime as dt

    return dt.datetime.fromtimestamp(stamp).isoformat(timespec="seconds")
