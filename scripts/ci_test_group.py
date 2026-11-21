"""跑一个**功能分组**的用例：CI 的每个 step 都只需要一行。

    python scripts/ci_test_group.py sku:rag                 # 跑 sku 侧 rag 组（带覆盖率）
    python scripts/ci_test_group.py image:worker --no-cov   # 不带覆盖率
    python scripts/ci_test_group.py --list                  # 列出所有 suite:group

它做四件事（顺序即 CI 里看到的效果）：

1. 从 `scripts/ci_split.py` 拿这一组的用例文件（分组只在那一处定义，CI 与本地镜像共用）；
2. 用 `COVERAGE_FILE=.coverage.<suite>.<group>` 跑 pytest —— 一个 job 里有多个 step，
   每个 step 写自己的分片，最后由 `coverage` job 统一 `coverage combine`（分片名都是
   `.coverage.*` 前缀，combine 自动收集）；
3. 输出边跑边打到 stdout（Actions 日志可读），同时落一份 `pytest-<suite>-<group>.log`；
4. 失败时调 `scripts/ci_annotate.py` 的注解逻辑，把「挂了哪些用例」写成公开可读的
   annotation —— workflow 原始日志要仓库权限才能下载，注解匿名可读。

退出码与 pytest 一致（0 通过 / 1 用例失败 / 其它为用法或收集错误）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ci_split  # noqa: E402 —— 同目录脚本，分组定义只此一处
from ci_annotate import annotate  # noqa: E402 —— 复用注解逻辑


def _run(argv: list[str], env: dict[str, str], log_path: Path) -> int:
    """跑子进程并按行回显 + 落日志。"""
    with subprocess.Popen(argv, cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                          errors="replace", bufsize=1) as proc, \
            log_path.open("w", encoding="utf-8") as log:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            log.write(line)
        return proc.wait()


def _groups() -> list[str]:
    return [f"{suite}:{slug}"
            for suite, spec in ci_split.SUITES.items()
            for slug, _title, _patterns in spec["groups"]
            if slug != "misc"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="按功能分组跑用例")
    parser.add_argument("target", nargs="?", help="SUITE:GROUP，例如 sku:rag")
    parser.add_argument("--no-cov", action="store_true", help="不带覆盖率（本地快速跑）")
    parser.add_argument("--list", action="store_true", help="列出所有 suite:group")
    args = parser.parse_args(argv[1:])

    if args.list or not args.target:
        for name in _groups():
            print(name)
        return 0
    if ":" not in args.target:
        print("[ci_test_group] 目标要写成 SUITE:GROUP，例如 sku:rag", file=sys.stderr)
        return 2

    suite, group = args.target.split(":", 1)
    files = ci_split.group_files(suite, group)
    title = next(t for slug, t, _p in ci_split.SUITES[suite]["groups"] if slug == group)

    env = dict(os.environ)
    shard = f".coverage.{suite}.{group}"
    argv_pytest = [sys.executable, "-m", "pytest", "-q", *files, "-rf"]
    if args.no_cov:
        env.pop("COVERAGE_FILE", None)
    else:
        env["COVERAGE_FILE"] = shard
        argv_pytest += ["--cov=src/app", "--cov-report="]

    log_path = ROOT / f"pytest-{suite}-{group}.log"
    print(f"===== {suite}:{group} —— {title}（{len(files)} 个用例文件）=====", flush=True)
    code = _run(argv_pytest, env, log_path)

    if code != 0:
        try:
            annotate(log_path.read_text(encoding="utf-8", errors="replace").splitlines())
        except Exception as exc:  # noqa: BLE001 —— 注解失败不能掩盖用例失败
            print(f"[ci_test_group] 注解失败：{exc}", file=sys.stderr)
    if code != 0 and args.no_cov:
        print(f"[ci_test_group] 日志：{log_path}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
