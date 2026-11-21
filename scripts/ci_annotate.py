"""把 pytest 的失败日志压成一条 GitHub Actions 注解（只在失败时调用）。

为什么要这个：workflow 的原始日志要仓库权限才能下载（`GET /actions/jobs/{id}/logs`
匿名访问返回 403 "Must have admin rights"），而注解（annotation）对公开仓库匿名可读
（`GET /check-runs/{id}/annotations`）。CI 里出问题、本地又复现不了时，
这是唯一不靠人肉贴日志的定位通道。

用法（CI 里跑完 pytest 之后）：
    python scripts/ci_annotate.py pytest-image.log

输出一行 `::error::<message>`，message 里的 `%` / CR / LF 已按 workflow command 规则转义。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 单条注解消息上限是 64KB，留足余量（转义后长度会膨胀，按原始字符数裁剪）。
MAX_CHARS = 12000
TAIL_LINES = 120
SUMMARY_LIMIT = 40


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _read(path: Path) -> list[str]:
    try:
        # utf-8-sig：pytest 日志可能是带 BOM 的 UTF-8（Windows 上重定向生成时常见）
        return path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError as exc:  # 日志文件缺失不该把 CI 再挂一次
        return [f"(无法读取日志 {path}: {exc})"]


def compose(lines: list[str]) -> str:
    """从 pytest 输出里挑出「哪些用例挂了」+「日志尾部」，拼成一条可读消息。"""
    failed = [ln.strip() for ln in lines if ln.startswith(("FAILED ", "ERROR "))]
    parts: list[str] = []
    if failed:
        shown = failed[:SUMMARY_LIMIT]
        header = f"失败用例 {len(failed)} 条"
        if len(failed) > len(shown):
            header += f"（只列前 {len(shown)} 条）"
        parts.append(header + "：\n" + "\n".join(shown))
    tail = [ln.rstrip() for ln in lines[-TAIL_LINES:]]
    parts.append(f"日志尾部（最后 {len(tail)} 行）：\n" + "\n".join(tail))
    return "\n\n".join(parts)[:MAX_CHARS]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: python scripts/ci_annotate.py <pytest 日志文件>", file=sys.stderr)
        return 2
    message = compose(_read(Path(argv[1])))
    # 直接写 UTF-8 字节：Windows 控制台的默认编码是 gbk，中文注解会 UnicodeEncodeError
    sys.stdout.buffer.write(("::error::" + _escape(message) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
