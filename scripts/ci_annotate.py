"""把 pytest 的失败日志压成若干条 GitHub Actions 注解（只在失败时调用）。

为什么要这个：workflow 的原始日志要仓库权限才能下载（匿名
`GET /actions/jobs/{id}/logs` 返回 403 "Must have admin rights"），而注解
（annotation）对公开仓库匿名可读（`GET /check-runs/{id}/annotations`）。
CI 里出问题、本地又复现不了时，这是唯一不靠人肉贴日志的定位通道。

**注解必须短**：runner 按行解析 workflow command，单行过长会被截断甚至整条丢弃
（实测一条 12k 字符的 `::error::` 完全没出现在 annotations 里）。所以这里
一条注解只放一行信息，条数靠"多行"堆出来：

    python scripts/ci_annotate.py pytest-image.log

输出（每行一条 command）：
    ::error::FAILED tests/test_x.py::test_y - AssertionError: ...
    ::warning::E   assert 0.42 < 0.15
"""

from __future__ import annotations

import sys
from pathlib import Path

# 单条注解的消息上限（runner 单行上限远小于此；只取关键一行）
MAX_MSG = 240
MAX_FAILED = 20
MAX_DETAIL = 10


def _escape(text: str) -> str:
    """workflow command 转义：% 必须最先替，之后才是 CR/LF。"""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _emit(level: str, message: str) -> None:
    print(f"::{level}::{_escape(message.strip()[:MAX_MSG])}", flush=True)


def _read(path: Path) -> list[str]:
    try:
        # utf-8-sig：日志可能是带 BOM 的 UTF-8（Windows 上重定向生成时常见）
        return path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError as exc:  # 日志缺失不该把 CI 再挂一次
        return [f"(无法读取日志 {path}: {exc})"]


def _is_failure_line(line: str) -> bool:
    return line.startswith(("FAILED ", "ERROR "))


def _is_detail_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(("E   ", "E  ", "assert ")) or "Error: " in stripped


def annotate(lines: list[str]) -> int:
    """返回写出的注解条数（调用方据此判断是否需要兜底）。"""
    written = 0
    failed = [ln.strip() for ln in lines if _is_failure_line(ln)]
    for line in failed[:MAX_FAILED]:
        _emit("error", line)
        written += 1
    details = [ln.strip() for ln in lines if _is_detail_line(ln)]
    # 末尾的断言行通常最接近真实原因
    for line in details[-MAX_DETAIL:]:
        _emit("warning", line)
        written += 1
    if not written:
        tail = " | ".join(ln.strip() for ln in lines[-6:] if ln.strip())
        _emit("error", f"pytest 失败但没解析到 FAILED 行；日志尾部：{tail}")
        written += 1
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: python scripts/ci_annotate.py <pytest 日志文件>", file=sys.stderr)
        return 2
    annotate(_read(Path(argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
