"""本地"CI 等效"运行器：按 `.github/workflows/gate.yml` 的 job 顺序执行同一批命令。

为什么需要它：GitHub Actions 只能在 push 之后跑，而提交前需要确认"推上去不会红"。
这里把 6 个 job 的步骤 1:1 镜像成本地命令（含按功能分组的用例 + 覆盖率分片 + combine 门槛），
任何一步失败都会停下并打印最后一段输出。

用例分组不在这里定义：`sku` / `image` 两组 job 逐个调用
`scripts/ci_test_group.py <suite>:<group>`，分组表在 `scripts/ci_split.py` 一处维护。

注意：镜像只能覆盖"命令层"。workflow 语义层（artifact 隐藏文件、COVERAGE_FILE 前缀、
artifact 名字配对、脚本引用、分组引用、分片前缀）由 `scripts/check_ci_config.py` 兜住，
`lint` job 会跑它。

用法：
    python scripts/ci_local.py                # 依次跑 lint → sku → image → coverage → eval
    python scripts/ci_local.py --job sku      # 只跑某个 job（lint|sku|image|coverage|eval|mysql）
    python scripts/ci_local.py --list         # 列出可用 job
    python scripts/ci_local.py --skip-eval    # 跳过耗时的评测门槛（本地快速自检）

约定：
- 与 CI 一致：`PYTHONPATH` 指向 `src/`；`PYTHONIOENCODING=utf-8`（Windows 控制台是 GBK）；
- 覆盖率分片按功能写 `.coverage.<suite>.<group>`，`coverage` job 用 `coverage combine` 合并；
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
SCRIPT_DIR = ROOT / "scripts"
PY = sys.executable

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ci_split  # noqa: E402 —— 分组定义的唯一事实源


def _image_files() -> list[str]:
    """生图侧的全部用例（保留给 `--job image` 之外的场景/排障用）。"""
    files = sorted(str(path.relative_to(ROOT)) for path in ROOT.glob("tests/test_image_*.py"))
    if not files:
        raise SystemExit("[ci_local] 没有匹配到生图用例：tests/test_image_*.py")
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
    "tests/test_platform_replenish_idem.py",
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
    # workflow 语义层自检：隐藏 artifact / COVERAGE_FILE 前缀 / artifact 名字 / 脚本引用 /
    # 分组引用（R7）/ 分片前缀（R8） / 覆盖率门槛
    _run("lint: ci_config", [PY, "scripts/check_ci_config.py"])
    # 分组自检：每个用例恰好属于一个功能面（新用例必须归类，否则 misc 非空 → 失败）
    _run("lint: ci_split", [PY, "scripts/ci_split.py", "--verify", "--strict"])
    if with_node and shutil.which("node"):
        _run("lint: ui_smoke", ["node", str(ROOT / "scripts" / "ui_smoke.js")])
        _run("lint: ui_smoke_image", ["node", str(ROOT / "scripts" / "ui_smoke_image.js")])
    elif with_node:
        print("[ci_local] 未安装 node，跳过前端冒烟（CI 会跑）")


def _pytest_groups(suite: str) -> None:
    """按 gate.yml 的顺序跑该 suite 的每个功能分组（每组一个覆盖率分片）。"""
    for slug, title, _patterns in ci_split.SUITES[suite]["groups"]:
        if slug == "misc" and not ci_split.group_files(suite, slug):
            continue
        _run(f"{suite}:{slug}（{title}）", [PY, "scripts/ci_test_group.py", f"{suite}:{slug}"])


def job_sku() -> None:
    _pytest_groups("sku")


def job_image() -> None:
    _pytest_groups("image")


def job_coverage() -> None:
    shards = sorted(ROOT.glob(".coverage.sku.*")) + sorted(ROOT.glob(".coverage.image.*"))
    if not shards:
        raise SystemExit("[ci_local] 没有覆盖率分片（先跑 --job sku / --job image）")
    suites = {path.name.split(".")[2] for path in shards}
    if {"sku", "image"} - suites:
        raise SystemExit(f"[ci_local] 分片不全（只有 {sorted(suites)}），两个 suite 都要跑")
    print(f"[ci_local] 合并 {len(shards)} 个分片")
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
    # 本地要有 MySQL 才跑得动（CI 用 service container），命令与 gate.yml 的 mysql job 一致
    _run("mysql: subset", [PY, "-m", "pytest", "-q", "-rf", *MYSQL_FILES])


JOBS = {
    "lint": job_lint,
    "sku": job_sku,
    "image": job_image,
    "coverage": job_coverage,
    "eval": job_eval,
    "mysql": job_mysql,
}
ORDER = ["lint", "sku", "image", "coverage", "eval"]


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
    job_sku()
    job_image()
    job_coverage()
    if not args.skip_eval:
        job_eval()
    if args.with_mysql:
        job_mysql()
    print("\n[ci_local] ALL GREEN（按 gate.yml 顺序）")
    return 0


if __name__ == "__main__":  # pragma: no cover —— CLI 入口
    raise SystemExit(main())
