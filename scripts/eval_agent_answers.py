"""答案质量评测：规则判定器 + 两种运行模式。

题集 `data/eval/agent_answers.jsonl` 用 `kind` 区分两类条目（复审 P3）：
- `live`：真实用户问法，`--live` 时真跑 Agent；
- `judge_sample`：给判定器用的**人工样例**（标题形如「（反例）…」，带 `sample` 与
  `expect_label`），只进离线自检，**不发**给模型——早先两类混跑，`--live` 的通过率被
  这几条必然失败的样例拉低，"实跑 10 条"也是假口径。

用法：
    # 1) 判定器自检（CI 用，完全离线、确定性）：拿题集里的人工样例跑判定，
    #    校验「该判过的判过、该判失败的判失败」——守住判定器本身不漂移
    PYTHONPATH=src python scripts/eval_agent_answers.py --offline

    # 2) 真跑 Agent（需要 DEEPSEEK_API_KEY，会花 token）：按题集逐条 run_chat，
    #    用同一套判定器打分，输出通过率与各维度指标
    PYTHONPATH=src python scripts/eval_agent_answers.py --live --limit 5

    # 3) 语义层复核（LLM-as-Judge，只判"是否忠于工具结果"，会花 token）
    PYTHONPATH=src python scripts/eval_agent_answers.py --live --judge llm

    # 4) 设阈值（用于发布前门槛）
    PYTHONPATH=src python scripts/eval_agent_answers.py --live --fail-under 0.8

为什么要有这个脚本：`eval_rag` 评检索、`eval_accuracy` 评筛查/研判标签，
但「对话答得对不对」一直没有量化门槛。grounding 只是单轮闸门，不是指标。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DATASET = ROOT / "data" / "eval" / "agent_answers.jsonl"


def load_dataset(limit: int | None = None, kind: str | None = None,
                 with_sample: bool = False) -> list[dict]:
    """读题集。

    `kind`：`live` = 真跑题（`--live` 用）；`judge_sample` = 判定器自检样例（标题就是
    「（反例）…」，不能发给模型）。复审 P3：早先 `--live` 把这 4 条自检样例也当真实问题
    发给模型，通过率被无意义拉低，「实跑 10 条」的口径也是假的。
    `with_sample`：只取带人工样例（`sample.reply` + `expect_label`）的，供离线自检。
    """
    cases: list[dict] = []
    with DATASET.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            if kind and (case.get("kind") or "live") != kind:
                continue
            if with_sample and not (case.get("sample") or {}).get("expect_label"):
                continue
            cases.append(case)
    return cases[:limit] if limit else cases


def _print_report(title: str, results: list[dict], agg: dict) -> None:
    print(f"\n== {title} ==")
    for item in results:
        mark = "ok  " if item["passed"] else "FAIL"
        print(f"  {mark} {item['id']:<5} {item.get('message', '')[:40]}")
        for issue in item["issues"]:
            print(f"        · {issue}")
        traj = item.get("trajectory")
        if traj:
            # 复审 E5：把这一轮"怎么走的"也打出来，避免评测只看"调没调工具"
            tools = "、".join(traj["tools"]) or "无"
            print(f"        ~ 轨迹：tools=[{tools}] 重复 {traj['repeat_calls']} · "
                  f"模型 {traj['llm_calls']} 次 · 预算 {traj['success_calls']}/"
                  f"{traj['max_calls'] or '-'} · agent={traj['agent'] or '-'}"
                  + (" · **空答复**" if traj.get("fallback") else ""))
    print(
        f"\n  通过 {agg['passed']}/{agg['cases']}（{agg['pass_rate'] * 100:.1f}%）"
    )
    for dim, stat in agg["dims"].items():
        if stat["checked"]:
            print(f"    {dim:<14} {stat['passed']}/{stat['checked']}"
                  f"（{stat['rate'] * 100:.0f}%）")
        else:
            print(f"    {dim:<14} 未检查")
    if agg["issues"]:
        print("  问题分布：")
        for key, count in agg["issues"].items():
            print(f"    {key} ×{count}")


def _mismatch(results: list[dict], cases: list[dict]) -> list[dict]:
    expected = {c["id"]: (c.get("sample") or {}).get("expect_label") for c in cases}
    return [
        {"id": r["id"], "expected": expected.get(r["id"]), "actual": r["label"]}
        for r in results
        if expected.get(r["id"]) and expected.get(r["id"]) != r["label"]
    ]


def run_offline(limit: int | None) -> int:
    """判定器自检：用题集里的人工样例跑判定，标签必须与 expect_label 一致。"""
    from app.agents.answer_eval import aggregate, judge

    cases = load_dataset(limit, with_sample=True)
    results = []
    for case in cases:
        sample = case.get("sample") or {}
        verdict = judge(case, sample.get("reply", ""), sample.get("tool_trace"), sample.get("steps"))
        verdict["id"] = case["id"]
        verdict["message"] = case.get("message", "")
        results.append(verdict)

    agg = aggregate(results)
    _print_report("判定器自检（人工样例）", results, agg)
    bad = _mismatch(results, cases)
    if bad:
        print("\n  ✗ 判定器与人工标签不一致：")
        for item in bad:
            print(f"    {item['id']}: 期望 {item['expected']} / 实际 {item['actual']}")
        return 1
    print("\n  判定器自检通过：所有人工样例的判定与标签一致。")
    return 0


def _run_one(case: dict, judge_mode: str = "rule") -> dict:
    from app.agents.answer_eval import judge, llm_judge
    from app.agents.chat_agent import run_chat
    from app.agents.trajectory import trajectory_of
    from app.auth import AuthUser
    from app.memory.runtime import chat_turn_context

    user = AuthUser(username="admin", role="admin", department=None)
    with chat_turn_context(session_id=None, include_query_plan=False):
        result = asyncio.run(run_chat(user, [], case["message"]))
    detail = result.get("run_detail") or {}
    verdict = judge(case, result.get("reply", ""), result.get("tool_trace"),
                    detail.get("steps"), run_detail=detail)
    verdict["id"] = case["id"]
    verdict["message"] = case.get("message", "")
    verdict["grounding"] = (detail.get("grounding") or {}).get("status")
    # 复审 E5：不只判"调没调工具"，把这轮**轨迹**也打出来（工具数/重复调用/预算/自检）
    trajectory = trajectory_of(detail, grounding_status=str(verdict["grounding"] or ""),
                               row_tool_calls=len(detail.get("steps") or []))
    verdict["trajectory"] = {
        "tools": [t for t in trajectory["tools"]],
        "repeat_calls": trajectory["repeat_calls"],
        "llm_calls": trajectory["llm_calls"],
        "success_calls": trajectory["success_calls"],
        "max_calls": trajectory["max_calls"],
        "agent": trajectory["agent_id"],
        # 复审 R3：这一轮模型有没有真的给出正文
        "fallback": bool(trajectory["is_fallback_answer"]),
    }
    if judge_mode == "llm":
        verdict["llm_judge"] = asyncio.run(
            llm_judge(case, result.get("reply", ""), result.get("tool_trace"))
        )
    return verdict


def run_live(limit: int | None, judge_mode: str = "rule") -> tuple[int, dict]:
    from app.agents.answer_eval import aggregate, aggregate_with_judge

    cases = load_dataset(limit, kind="live")
    print(f"\n  只跑 kind=live 的题（判定器自检样例不发给模型）：{len(cases)} 条")
    results = [_run_one(case, judge_mode) for case in cases]
    agg = aggregate_with_judge(results) if judge_mode == "llm" else aggregate(results)
    _print_report("Agent 实跑", results, agg)
    grounding = [r.get("grounding") for r in results]
    print(f"\n  出口自检分布：{ {g: grounding.count(g) for g in set(grounding)} }")
    if agg.get("llm_judge"):
        judge_stat = agg["llm_judge"]
        if judge_stat["checked"]:
            print(f"  LLM-as-Judge：判过 {judge_stat['checked']} 条，忠实 {judge_stat['faithful']} 条，"
                  f"平均分 {judge_stat['avg_score']}")
        else:
            print("  LLM-as-Judge：未判（模型不可用，已降级为仅规则判定）")
    return 0, agg


def main() -> int:
    parser = argparse.ArgumentParser(description="对话答案质量评测（规则判定器）")
    parser.add_argument("--offline", action="store_true", help="只跑判定器自检（默认）")
    parser.add_argument("--live", action="store_true", help="真跑 Agent（需要 API Key）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    parser.add_argument("--judge", choices=["rule", "llm"], default="rule",
                        help="判定方式：rule=规则判定器（默认，零成本）/ llm=再跑一次 LLM-as-Judge")
    parser.add_argument("--fail-under", type=float, default=None,
                        help="通过率低于该值则以非 0 退出（发布门槛）")
    parser.add_argument("--json-out", default="", help="把明细写到该 JSON 文件")
    args = parser.parse_args()

    if args.live:
        code, agg = run_live(args.limit, args.judge)
        payload = agg
    else:
        code = run_offline(args.limit)
        payload = {"mode": "offline", "exit": code}

    if args.json_out:
        pathlib.Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n  明细已写入 {args.json_out}")

    if args.fail_under is not None and args.live:
        rate = payload.get("pass_rate", 0.0)
        if rate < args.fail_under:
            print(f"\n  ✗ 通过率 {rate * 100:.1f}% 低于阈值 {args.fail_under * 100:.0f}%")
            return 1
        print(f"\n  ✓ 通过率 {rate * 100:.1f}% ≥ 阈值 {args.fail_under * 100:.0f}%")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
