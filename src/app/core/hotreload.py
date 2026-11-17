"""热加载指纹：判断「目录里的文件有没有变」，供注册表 / 提示词 / 知识库使用。

指纹 = (目录 mtime, 目录内文件的最大 mtime, 文件清单摘要)。
为什么不是只看 mtime：文件系统的 mtime 粒度不一定比两次写入更细。容器 overlayfs 上
实测出现过「新建文件与上一次写落在同一时间片、mtime 完全相同」，只比 mtime 会漏掉
新增 / 改名。把「文件名 + 大小 + mtime_ns」的 sha1 并进指纹后，这类变更也能确定性检出。
文件数很少（< 50），每次 stat 一遍的成本可以忽略。
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Fingerprint:
    dir_mtime: float
    max_file_mtime: float
    listing: str

    def as_tuple(self) -> tuple[float, float, str]:
        return (self.dir_mtime, self.max_file_mtime, self.listing)


def fingerprint(directory: pathlib.Path | str, pattern: str = "*") -> Fingerprint:
    """算目录指纹；目录不存在时返回全零指纹（视为「空」而不是报错）。"""
    base = pathlib.Path(directory)
    try:
        dir_mtime = base.stat().st_mtime
    except OSError:
        return Fingerprint(0.0, 0.0, "")

    items: list[str] = []
    max_mtime = 0.0
    try:
        paths = sorted(base.glob(pattern))
    except OSError:
        paths = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        items.append(f"{path.name}:{st.st_size}:{st.st_mtime_ns}")
        max_mtime = max(max_mtime, st.st_mtime)
    listing = hashlib.sha1("|".join(items).encode("utf-8")).hexdigest()[:12] if items else ""
    return Fingerprint(dir_mtime, max_mtime, listing)


class HotReloader:
    """「变了才重载」的轻量封装：每轮调用 `maybe_reload()`，只在指纹变化时执行回调。"""

    def __init__(self, directory: pathlib.Path | str, pattern: str = "*") -> None:
        self.directory = pathlib.Path(directory)
        self.pattern = pattern
        self._fp: Fingerprint | None = None

    @property
    def current(self) -> Fingerprint:
        return fingerprint(self.directory, self.pattern)

    def changed(self) -> bool:
        """只判断是否变了，不更新指纹（用于「先看再决定」）。"""
        if self._fp is None:
            return True
        return self.current != self._fp

    def mark(self) -> Fingerprint:
        self._fp = self.current
        return self._fp

    def maybe_reload(self, on_change: Callable[[], None]) -> bool:
        """指纹变化 → 调用回调并刷新指纹，返回 True；无变化返回 False。"""
        if not self.changed():
            return False
        self.mark()
        on_change()
        return True

    def reset(self) -> None:
        self._fp = None
