"""CI 配置自检：把「只会在 GitHub 上才炸」的几类坑变成本地 lint 规则。

背景（都是真实踩过的，见 `docs/myself/技术债.md` §十三）：

- `actions/upload-artifact` v4.4+ **默认跳过隐藏文件**，而覆盖率分片叫 `.coverage.sku`，
  于是「pytest 全绿、job 挂在上传」；
- `coverage combine` 按 `COVERAGE_FILE` 的前缀找分片，workflow 顶层一旦设了
  `COVERAGE_FILE: .coverage.default`，就只找 `.coverage.default.*`，找不到 →
  `No data to combine`；
- 新加的步骤引用 `scripts/xxx.py` 时打错文件名，只有跑到那一步才会 500。

这些都不是代码逻辑问题，而是**workflow 的语义**问题，本地 `ci_local.py` 抄一份命令
是抄不出这些约定的。所以这里直接读 `ci.yml` 做静态断言：单一事实源就是 YAML 本身。

用法：
    python scripts/check_ci_config.py                 # 默认查 .github/workflows/ci.yml
    python scripts/check_ci_config.py path/to/ci.yml
    python scripts/check_ci_config.py --list          # 只列出规则

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
DEFAULT_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


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


CHECKS = (check_artifact_hidden, check_coverage_prefix, check_artifact_parity,
          check_script_refs, check_needs, check_coverage_threshold)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="CI 配置自检（workflow 语义层）")
    parser.add_argument("path", nargs="?", default=str(DEFAULT_WORKFLOW))
    parser.add_argument("--list", action="store_true", help="只列出规则")
    args = parser.parse_args(argv[1:])

    if args.list:
        for rule, text in RULES:
            print(f"{rule}: {text}")
        return 0

    path = Path(args.path)
    if not path.is_file():
        print(f"[ci-config] 找不到 workflow：{path}", file=sys.stderr)
        return 2
    workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    jobs = workflow.get("jobs") or {}

    violations: list[Violation] = []
    for check in CHECKS:
        violations.extend(check(workflow, jobs))

    if violations:
        print(f"[ci-config] ✗ {path.name}：{len(violations)} 处违反", file=sys.stderr)
        for violation in violations:
            print("  " + str(violation), file=sys.stderr)
        return 1
    print(f"[ci-config] ✓ {path.name}：{len(CHECKS)} 条规则全过（{len(jobs)} 个 job）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
