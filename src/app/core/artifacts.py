"""产物落盘与过期清理：原子写 + sha256 去重 + 按小时清理（供图片存储复用）。"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import pathlib
import time


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_dir(path: pathlib.Path | str) -> pathlib.Path:
    base = pathlib.Path(path)
    base.mkdir(parents=True, exist_ok=True)
    return base


def atomic_write(path: pathlib.Path | str, data: bytes | str) -> pathlib.Path:
    """同目录临时文件 + fsync + os.replace：任何时刻读到的都是完整文件。"""
    target = pathlib.Path(path)
    ensure_dir(target.parent)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}")
    payload = data.encode("utf-8") if isinstance(data, str) else data
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return target


def sweep_older_than(directory: pathlib.Path | str, hours: float,
                     pattern: str = "*", now: dt.datetime | None = None) -> int:
    """删除目录下 mtime 早于 `now - hours` 的文件，返回删除数（目录不存在返回 0）。"""
    base = pathlib.Path(directory)
    if not base.exists():
        return 0
    cutoff = (now or dt.datetime.now()) - dt.timedelta(hours=hours)
    removed = 0
    for path in base.glob(pattern):
        if not path.is_file():
            continue
        try:
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed
