"""本地"CI 等效"运行器：按 `.github/workflows/verify.yml` 的 job 顺序执行同一批命令。

为什么需要它：GitHub Actions 只能在 push 之后跑，而提交前需要确认"推上去不会红"。
这里把 6 个 job 的步骤 1:1 镜像成本地命令（含覆盖率分片 + combine 门槛），
任何一步失败都会停下并打印最后一段输出。
注意：镜像只能覆盖"命令层"。workflow 语义层（artifact 隐藏文件、COVERAGE_FILE 前缀、
artifact 名字配对、脚本引用是否存在）由 `scripts/check_ci_config.py` 兜住，`lint` job 会跑它。

用法：
    python scripts/ci_local.py                # 依次跑 lint → test-sku → test-image → coverage → eval
    python scripts/ci_local.py --job lint     # 只跑某个 job（lint|test-sku|test-image|coverage|eval）
    python scripts/ci_local.py --list         # 列出可用 job
    python scripts/ci_local.py --skip-eval    # 跳过耗时的评测门槛（本地快速自检）

约定：
- 与 CI 一致：`PYTHONPATH` 指向 `src/`；`PYTHONIOENCODING=utf-8`（Windows 控制台是 GBK）；
- 覆盖率分片写到 `.ci-coverage/`（.gitignore 已忽略 `.coverage*` 与工作目录外文件）；
- MySQL job 需要容器，本地默认不跑（用 `--with-mysql` 显式开启，先起 composer service）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".ci-coverage"
PY = sys.executable

SKU_IGNORE = "tests/test_image_*.py"
IMAGE_GLOB = "tests/test_image_*.py"


def _image_files() -> list[str]:
    """自己展开 glob：CI 上是 bash 展开，本地 subprocess 不走 shell，必须由脚本展开。"""
    files = sorted(str(path.relative_to(ROOT)) for path in ROOT.glob(IMAGE_GLOB))
    if not files:
        raise SystemExit(f"[ci_local] 没有匹配到生图用例：{IMAGE_GLOB}")
    return files

MYSQL_FILES = [
    "tests/test_analysis_idempotent.py",
    "tests/test_analysis_concurrency.py",
    "tests/test_patrol.py",
    "tests/test_api.py",
    "tests/test_chat_store.py",
    "tests/test_memory_layers.py",
    "tests/test_rag_index_reconcile.py",
    "tests/test_screening.py",
    "tests/test_metrics_cards.py",
    "tests/test_platform.py",
    "tests/test_platform_authz.py",
    "tests/test_platform_governance.py",
    "tests/test_demo_catalog_contract.py",
]

EVAL_STEPS = [
    ("init_db", ["scripts/init_db.py"]),
    ("eval_agent_answers", ["scripts/eval_agent_answers.py", "--offline"]),
    ("eval_rag", ["scripts/eval_rag.py", "--min-recall", "0.8"]),
    ("eval_routing", ["scripts/eval_routing.py", "--min-accuracy", "0.9"]),
    ("eval_permissions", ["scripts/eval_permissions.py", "--min-pass", "1.0"]),
    ("eval_grounding", ["scripts/eval_grounding.py", "--max-false-positive", "0.1"]),
    ("eval_platform_governance", ["scripts/eval_platform_governance.py", "--min-pass", "1.0"]),
    ("eval_agent_tooling", ["scripts/eval_agent_tooling.py", "--min-pass", "1.0"]),
    ("verify_concurrent_writes", ["scripts/verify_concurrent_writes.py"]),
]


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run(name: str, argv: list[str], *, cwd: Path | None = None) -> None:
    print(f"\n===== {name} =====", flush=True)
    print("$ " + " ".join(argv), flush=True)
    proc = subprocess.run(argv, cwd=str(cwd or ROOT), env=_env())
    if proc.returncode != 0:
        raise SystemExit(f"[ci_local] {name} 失败（exit={proc.returncode}）")


def job_lint(*, with_node: bool = True) -> None:
    _run("lint: ruff", [PY, "-m", "ruff", "check", "src", "tests", "scripts"])
    _run("lint: compileall", [PY, "-m", "compileall", "-q", "src", "scripts"])
    # workflow 语义层自检：隐藏 artifact / COVERAGE_FILE 前缀 / artifact 名字 / 脚本引用 / 门槛
    _run("lint: ci_config", [PY, "scripts/check_ci_config.py"])
    if with_node and shutil.which("node"):
        _run("lint: ui_smoke", ["node", str(ROOT / "scripts" / "ui_smoke.js")])
        _run("lint: ui_smoke_image", ["node", str(ROOT / "scripts" / "ui_smoke_image.js")])
    elif with_node:
        print("[ci_local] 未安装 node，跳过前端冒烟（CI 会跑）")


def _pytest_split(name: str, select_args: list[str]) -> None:
    """跑一半用例并把覆盖率写到 `.<name>` 分片（与 CI 的 COVERAGE_FILE 一致）。"""
    data_file = ROOT / f".coverage.{name}"
    data_file.unlink(missing_ok=True)
    env = _env()
    env["COVERAGE_FILE"] = str(data_file)
    print(f"\n===== {name} =====", flush=True)
    print("$ " + " ".join([PY, "-m", "pytest", "-q", *select_args]), flush=True)
    proc = subprocess.run([PY, "-m", "pytest", "-q", *select_args,
                           "--cov=src/app", "--cov-report="],
                          cwd=str(ROOT), env=env)
    if proc.returncode != 0:
        raise SystemExit(f"[ci_local] {name} 失败（exit={proc.returncode}）")
    if not data_file.is_file():
        raise SystemExit(f"[ci_local] {name} 没有产出覆盖率分片：{data_file}")


def job_test_sku() -> None:
    _pytest_split("sku", [f"--ignore-glob={SKU_IGNORE}"])


def job_test_image() -> None:
    _pytest_split("image", _image_files())


def job_coverage() -> None:
    shards = [ROOT / ".coverage.sku", ROOT / ".coverage.image"]
    missing = [str(path) for path in shards if not path.is_file()]
    if missing:
        raise SystemExit(f"[ci_local] 缺少覆盖率分片：{missing}（先跑 test-sku / test-image）")
    merged = ROOT / ".coverage"
    merged.unlink(missing_ok=True)
    _run("coverage: combine", [PY, "-m", "coverage", "combine"])
    _run("coverage: report（≥80%）",
         [PY, "-m", "coverage", "report", "--fail-under=80", "--show-missing"])


def job_eval() -> None:
    # CI 是全新 checkout；本地重复跑会把"已经播过种"的库再播一遍（UNIQUE 冲突）。
    # 除非显式设置 CI_LOCAL_KEEP_DB=1，这里先清掉演示库，等价于 CI 的干净环境。
    if not os.environ.get("CI_LOCAL_KEEP_DB"):
        for pattern in ("data/sku_demo.db", "data/sku_demo.db-*", "data/checkpoints.sqlite*"):
            for path in ROOT.glob(pattern):
                path.unlink(missing_ok=True)
    for name, args in EVAL_STEPS:
        _run(f"eval: {name}", [PY, *args])


def job_mysql() -> None:
    _run("mysql: subset", [PY, "-m", "pytest", "-q", *MYSQL_FILES])


JOBS = {
    "lint": job_lint,
    "test-sku": job_test_sku,
    "test-image": job_test_image,
    "coverage": job_coverage,
    "eval": job_eval,
    "mysql": job_mysql,
}
ORDER = ["lint", "test-sku", "test-image", "coverage", "eval"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地按 CI job 顺序跑一遍")
    parser.add_argument("--job", default="", help="只跑一个 job（见 --list）")
    parser.add_argument("--list", action="store_true", help="列出 job")
    parser.add_argument("--skip-eval", action="store_true", help="跳过评测门槛（快速自检）")
    parser.add_argument("--with-mysql", action="store_true",
                        help="额外跑 MySQL 子集（需要本地 3306 的 MySQL）")
    parser.add_argument("--no-node", action="store_true", help="跳过前端 DOM 冒烟")
    args = parser.parse_args(argv)

    if args.list:
        print("jobs:", "、".join(ORDER + ["mysql"]))
        return 0

    if args.job:
        if args.job not in JOBS:
            print(f"未知 job：{args.job}（可选：{'、'.join(ORDER + ['mysql'])}）")
            return 2
        if args.job == "lint":
            job_lint(with_node=not args.no_node)
        else:
            JOBS[args.job]()
        print("\n[ci_local] OK:", args.job)
        return 0

    job_lint(with_node=not args.no_node)
    job_test_sku()
    job_test_image()
    job_coverage()
    if not args.skip_eval:
        job_eval()
    if args.with_mysql:
        job_mysql()
    print("\n[ci_local] ALL GREEN（按 verify.yml 顺序）")
    return 0


if __name__ == "__main__":  # pragma: no cover —— CLI 入口
    raise SystemExit(main())
