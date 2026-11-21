"""CI 配置自检：把「只会在 GitHub 上才炸」的几类坑变成本地 lint 规则。

背景（都是真实踩过的，见 `docs/myself/技术债.md` §十三/§十四）：

- `actions/upload-artifact` v4.4+ **默认跳过隐藏文件**，而覆盖率分片叫 `.coverage.sku`，
  于是「pytest 全绿、job 挂在上传」；
- `coverage combine` 按 `COVERAGE_FILE` 的前缀找分片，workflow 顶层一旦设了
  `COVERAGE_FILE: .coverage.default`，就只找 `.coverage.default.*`，找不到 →
  `No data to combine`；
- 新加的步骤引用 `scripts/xxx.py` 时打错文件名，只有跑到那一步才会炸。

这些都不是代码逻辑问题，而是**workflow 的语义**问题，本地 `ci_local.py` 抄一份命令
是抄不出这些约定的。所以这里直接读 workflow YAML 做静态断言：单一事实源就是 YAML 本身。

用法：
    python scripts/check_ci_config.py                    # 默认查 .github/workflows/*.yml
    python scripts/check_ci_config.py path/to/one.yml     # 只查一个文件
    python scripts/check_ci_config.py path/to/workflows/  # 查一个目录下的所有 yml/yaml
    python scripts/check_ci_config.py --list              # 只列出规则

退出码：0 = 全部通过；1 = 有规则被违反（每条都会打印规则号 + job/step 定位）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"


def _force_utf8() -> None:
    """Windows 控制台默认 gbk，直接 print「✓」会 UnicodeEncodeError（CI 上是 UTF-8）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


_force_utf8()

# 覆盖率门槛口径（历史约定：不降）。改这个值要单独评审。
COVERAGE_FAIL_UNDER = 80

RULES: list[tuple[str, str]] = [
    ("R1", "upload-artifact 上传隐藏文件（点开头）时必须 include-hidden-files: true"),
    ("R2", "跑 coverage combine/report 的步骤不能带非默认 COVERAGE_FILE 前缀"),
    ("R3", "download-artifact 用 pattern 下载时，needs 里的每个 job 都要上传匹配的 artifact"),
    ("R4", "run 里引用的 scripts/*.py、scripts/*.js 必须真实存在"),
    ("R5", "job 的 needs 必须指向存在的 job"),
    ("R6", "coverage report 的门槛不得低于 80%"),
    ("R7", "ci_test_group.py 引用的 suite:group 必须存在且非空（分组定义在 ci_split.py）"),
    ("R8", "覆盖率分片的 artifact 路径必须与该 job 实际写入的分片前缀一致"),
]


class Violation:
    def __init__(self, rule: str, where: str, detail: str) -> None:
        self.rule = rule
        self.where = where
        self.detail = detail

    def __str__(self) -> str:
        return f"[{self.rule}] {self.where}：{self.detail}"


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps") or []
    return [step for step in steps if isinstance(step, dict)]


def _uses(step: dict[str, Any]) -> str:
    return str(step.get("uses") or "")


def _with(step: dict[str, Any]) -> dict[str, Any]:
    value = step.get("with")
    return value if isinstance(value, dict) else {}


def _run(step: dict[str, Any]) -> str:
    return str(step.get("run") or "")


def _env_chain(workflow: dict[str, Any], job: dict[str, Any], step: dict[str, Any]) -> dict[str, str]:
    """按 workflow → job → step 的顺序合并 env（后面的覆盖前面的）。"""
    merged: dict[str, str] = {}
    for source in (workflow.get("env"), job.get("env"), step.get("env")):
        if isinstance(source, dict):
            merged.update({str(k): str(v) for k, v in source.items()})
    return merged


def _label(job_id: str, step: dict[str, Any], index: int) -> str:
    name = step.get("name") or _uses(step) or _run(step).splitlines()[0][:40] or f"step#{index}"
    return f"job {job_id} / {name}"


def check_artifact_hidden(workflow: dict[str, Any], jobs: dict[str, Any]) -> list[Violation]:
    """R1：路径以点开头 = 隐藏文件，upload-artifact v4.4+ 默认跳过。"""
    out: list[Violation] = []
    for job_id, job in jobs.items():
        for index, step in enumerate(_steps(job or {})):
            if "upload-artifact" not in _uses(step):
                continue
            raw = _with(step).get("path", "")
            paths = [raw] if isinstance(raw, str) else [str(item) for item in raw or []]
            hidden = [p for p in paths if p.strip().startswith(".")]
            if not hidden:
                continue
            include = _with(step).get("include-hidden-files", False)
            if str(include).strip().lower() not in ("true", "1", "yes"):
                out.append(Violation(
                    "R1", _label(job_id, step, index),
                    f"path={hidden} 是隐藏文件，但没设 include-hidden-files: true"
                    "（upload-artifact v4.4+ 会跳过它，报 No files were found）"))
    return out


def check_coverage_prefix(workflow: dict[str, Any], jobs: dict[str, Any]) -> list[Violation]:
    """R2：`coverage combine` 按 COVERAGE_FILE 前缀找分片，非默认值会让它找不到。"""
    out: list[Violation] = []
    for job_id, job in jobs.items():
        for index, step in enumerate(_steps(job or {})):
            run = _run(step)
            if "coverage combine" not in run and "coverage report" not in run:
                continue
            if "--data-file" in run:            # 显式指定数据文件时前缀无所谓
                continue
            data_file = _env_chain(workflow, job or {}, step).get("COVERAGE_FILE", "")
            if data_file.strip() in ("", ".coverage"):
                continue
            out.append(Violation(
                "R2", _label(job_id, step, index),
                f"COVERAGE_FILE={data_file} 会把 combine 的查找前缀改成 "
                f"`{data_file}.*`，两个分片都匹配不到（报 No data to combine）"))
    return out


def check_artifact_parity(workflow: dict[str, Any], jobs: dict[str, Any]) -> list[Violation]:
    """R3：`pattern: coverage-*` 下载的 artifact，needs 里**每个** job 都得按这个前缀上传。

    只要求「有人上传」是不够的：少传一个分片时 combine 只会合并剩下的那份，
    覆盖率可能照样过门槛，等于静默缩小了统计范围。
    """
    out: list[Violation] = []
    for job_id, job in jobs.items():
        job = job or {}
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else list(needs)
        for index, step in enumerate(_steps(job)):
            if "download-artifact" not in _uses(step):
                continue
            pattern = str(_with(step).get("pattern") or "")
            if not pattern.endswith("*"):
                continue
            prefix = pattern.rstrip("*")
            for need in needs:
                uploaded = [str(_with(other).get("name") or "")
                            for other in _steps(jobs.get(need) or {})
                            if "upload-artifact" in _uses(other)]
                if not any(name.startswith(prefix) for name in uploaded):
                    out.append(Violation(
                        "R3", _label(job_id, step, index),
                        f"pattern={pattern} 需要 {need} 上传匹配前缀的 artifact，"
                        f"但它实际只上传了 {uploaded or '（没有上传步骤）'}"))
    return out


_PY_REF = re.compile(r"scripts/[A-Za-z0-9_./-]+\.(?:py|js)")
_Q = "['\"]"


def check_script_refs(workflow: dict[str, Any], jobs: dict[str, Any]) -> list[Violation]:
    """R4：run 里点名的脚本必须存在（打错文件名只在跑到那一步时才炸）。"""
    out: list[Violation] = []
    for job_id, job in jobs.items():
        for index, step in enumerate(_steps(job or {})):
            for match in _PY_REF.finditer(_run(step)):
                rel = match.group(0)
                if not (ROOT / rel).is_file():
                    out.append(Violation("R4", _label(job_id, step, index),
                                         f"引用的 {rel} 不存在"))
    return out


def check_needs(workflow: dict[str, Any], jobs: dict[str, Any]) -> list[Violation]:
    """R5：needs 指向不存在的 job，GitHub 会在解析阶段就报错。"""
    out: list[Violation] = []
    for job_id, job in jobs.items():
        needs = (job or {}).get("needs") or []
        needs = [needs] if isinstance(needs, str) else list(needs)
        for need in needs:
            if need not in jobs:
                out.append(Violation("R5", f"job {job_id}", f"needs 指向不存在的 job {need}"))
    return out


def check_coverage_threshold(workflow: dict[str, Any], jobs: dict[str, Any]) -> list[Violation]:
    """R6：覆盖率门槛不许降（历史口径 ≥ 80%）。"""
    out: list[Violation] = []
    for job_id, job in jobs.items():
        for index, step in enumerate(_steps(job or {})):
            run = _run(step)
            if "coverage report" not in run:
                continue
            found = re.findall(r"--fail-under[= ]([0-9.]+)", run)
            if not found:
                out.append(Violation("R6", _label(job_id, step, index),
                                     "coverage report 没带 --fail-under，门槛形同虚设"))
                continue
            for value in found:
                if float(value) < COVERAGE_FAIL_UNDER:
                    out.append(Violation("R6", _label(job_id, step, index),
                                         f"--fail-under={value} 低于 {COVERAGE_FAIL_UNDER}%"))
    return out


_GROUP_REF = re.compile(r"ci_test_group\.py\s+([A-Za-z0-9_]+:[A-Za-z0-9_]+)")

def _ci_split():
    """分组定义的唯一事实源（同目录脚本）。"""
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import ci_split  # noqa: PLC0415 —— 延迟导入：只有 R7/R8 需要

    return ci_split


def check_test_groups(workflow: dict[str, Any], jobs: dict[str, Any]) -> list[Violation]:
    """R7：`ci_test_group.py suite:group` 引用的分组必须存在、且该组有用例。"""
    out: list[Violation] = []
    try:
        split = _ci_split()
    except Exception as exc:  # noqa: BLE001 —— 分组脚本坏了要报出来，而不是静默跳过
        return [Violation("R7", "scripts/ci_split.py", f"无法导入分组定义：{exc}")]
    for job_id, job in jobs.items():
        for index, step in enumerate(_steps(job or {})):
            for target in _GROUP_REF.findall(_run(step)):
                suite, group = target.split(":", 1)
                spec = split.SUITES.get(suite)
                if spec is None:
                    out.append(Violation("R7", _label(job_id, step, index),
                                         f"未知 suite {suite}（可用：{', '.join(split.SUITES)}）"))
                    continue
                slugs = [slug for slug, _title, _patterns in spec["groups"]]
                if group not in slugs:
                    out.append(Violation("R7", _label(job_id, step, index),
                                         f"{suite} 没有分组 {group}（可用：{', '.join(slugs)}）"))
                    continue
                if not split.group_files(suite, group):
                    out.append(Violation("R7", _label(job_id, step, index),
                                         f"分组 {target} 一个用例都没匹配到"))
    return out


def check_coverage_shards(workflow: dict[str, Any], jobs: dict[str, Any]) -> list[Violation]:
    """R8：job 里上传的 `.coverage.<suite>.*` 必须与它实际跑的分组 suite 对得上。

    `ci_test_group.py` 写的分片名是 `.coverage.<suite>.<group>`；如果 workflow 里
    把 path 写成另一种前缀（或漏了通配），artifact 会是空的、门槛 job 直接失败。
    """
    out: list[Violation] = []
    for job_id, job in jobs.items():
        steps = _steps(job or {})
        suites = {target.split(":", 1)[0]
                  for step in steps for target in _GROUP_REF.findall(_run(step))}
        if not suites:
            continue
        for index, step in enumerate(steps):
            if "upload-artifact" not in _uses(step):
                continue
            raw = _with(step).get("path", "")
            paths = [raw] if isinstance(raw, str) else [str(item) for item in raw or []]
            for suite in suites:
                if not any(str(p).startswith(f".coverage.{suite}.") for p in paths):
                    out.append(Violation(
                        "R8", _label(job_id, step, index),
                        f"路径 {paths} 与 job 实际写入的 `.coverage.{suite}.*` 分片不匹配"))
    return out


# 规则注册表（放在各 check 函数之后：CHECKS 引用的是函数对象）
CHECKS = (check_artifact_hidden, check_coverage_prefix, check_artifact_parity,
          check_script_refs, check_needs, check_coverage_threshold,
          check_test_groups, check_coverage_shards)


def _workflow_files(target: Path) -> list[Path]:
    """目标可以是单个文件，也可以是目录（目录下所有 .yml/.yaml）。"""
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted([*target.glob("*.yml"), *target.glob("*.yaml")])
    return []


def check_file(path: Path) -> tuple[list[str], int]:
    """跑完一个 workflow 的全部规则，返回（错误描述列表, job 数）。"""
    workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    jobs = workflow.get("jobs") or {}
    violations: list[Violation] = []
    for check in CHECKS:
        violations.extend(check(workflow, jobs))
    return [str(item) for item in violations], len(jobs)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="CI 配置自检（workflow 语义层）")
    parser.add_argument("path", nargs="?", default=str(WORKFLOW_DIR))
    parser.add_argument("--list", action="store_true", help="只列出规则")
    args = parser.parse_args(argv[1:])

    if args.list:
        for rule, text in RULES:
            print(f"{rule}: {text}")
        return 0

    target = Path(args.path)
    files = _workflow_files(target)
    if not files:
        print(f"[ci-config] 找不到 workflow：{target}", file=sys.stderr)
        return 2

    failed = 0
    for path in files:
        violations, job_count = check_file(path)
        if violations:
            failed += 1
            print(f"[ci-config] ✗ {path.name}：{len(violations)} 处违反", file=sys.stderr)
            for violation in violations:
                print("  " + violation, file=sys.stderr)
        else:
            print(f"[ci-config] ✓ {path.name}：{len(CHECKS)} 条规则全过（{job_count} 个 job）")
    if failed:
        print(f"[ci-config] 共 {failed}/{len(files)} 个 workflow 有违反", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
