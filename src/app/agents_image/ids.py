"""任务 ID：`YYYYMMDD`(8) + `HHMMSS`(6) + 4 位随机 hex = 18 位，可读、可排序、无状态。

示例：`20261117153045a3f9`；正则 `^\\d{14}[0-9a-f]{4}$`。
"""

from __future__ import annotations

import datetime as dt
import re
import secrets

TASK_ID_RE = re.compile(r"^\d{14}[0-9a-f]{4}$")


def new_task_id(now: dt.datetime | None = None, *, max_tries: int = 8) -> str:
    """同一秒内并发时用 4 位随机 hex 拉开；重复概率极低，重试即可。"""
    moment = now or dt.datetime.now()
    prefix = moment.strftime("%Y%m%d%H%M%S")
    for _ in range(max_tries):
        candidate = f"{prefix}{secrets.token_hex(2)}"
        if TASK_ID_RE.match(candidate):
            return candidate
    raise RuntimeError("生成任务 ID 失败")  # pragma: no cover


def is_valid_task_id(value: str) -> bool:
    return bool(TASK_ID_RE.match(value or ""))


def new_batch_id(now: dt.datetime | None = None) -> str:
    """批量请求 id（一次请求选了多个商品）：与任务 ID 同规则同格式，只是不复用任务 id 命名空间。"""
    return new_task_id(now)


__all__ = ["TASK_ID_RE", "new_task_id", "new_batch_id", "is_valid_task_id"]
