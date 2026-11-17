"""Agent 路由评测：golden set 上的准确率（确定性路由，零 token）。

用法：
    PYTHONPATH=src python scripts/eval_routing.py                # 打印逐条结果与准确率
    PYTHONPATH=src python scripts/eval_routing.py --min-accuracy 0.85   # 作为 CI 门槛

为什么单独评测路由：多 Agent 的第一道风险不是"某个 agent 答错"，而是"问题被派给了错的 agent"。
路由是确定性规则，可以像检索一样做离线回归——改关键词/加 agent 前后都跑一遍。

P2-5 起每条用例还可以声明**能力断言**：
- `expect_tools`：被派到的 Agent 必须能调这些工具（否则"派对了也答不了"）；
- `forbid_tools`：只读 Agent 不得具备这些工具（写操作/沉淀档案）。
这类缺陷路由指标永远抓不到（agent_id 明明是对的），所以要在同一套题集里一起回归。

P5 起用例还可以声明 `plan_intent`：声明"这句话会被 query_plan 解析成什么意图"，
评测按生产路径（`resolve_agent` → `registry.route(message, plan)`）一起跑，
于是 manifest 的 `routing.intents` 分支也在回归范围内（早先只传 message，该分支永远是死代码）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DATASET = ROOT / "data" / "eval" / "routing.jsonl"


def load_cases(limit: int | None = None) -> list[dict]:
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    return cases[:limit] if limit else cases


def tool_capability_misses(manifest, case: dict) -> list[str]:  # noqa: ANN001 —— AgentManifest
    """校验「派到的 Agent 是否具备完成任务所需的工具」。

    `tool_names == ["*"]` 表示全量工具：`expect_tools` 视为满足，`forbid_tools` 视为违反
    （全量 Agent 确实带写工具——它靠账号权限拦截，不在 manifest 层收窄）。
    """
    if manifest is None:
        return ["Agent 未注册，无法校验工具能力"]
    tools = set(manifest.tool_names)
    wildcard = tools == {"*"}
    misses: list[str] = []
    for name in case.get("expect_tools") or []:
        if not wildcard and name not in tools:
            misses.append(f"缺少工具 {name}")
    for name in case.get("forbid_tools") or []:
        if wildcard or name in tools:
            misses.append(f"不该具备工具 {name}")
    return misses


def evaluate() -> tuple[list[dict], float]:
    from app.agents.registry import get_registry

    registry = get_registry()
    registry.reload()
    results: list[dict] = []
    for case in load_cases():
        # 复审 P5：生产里 `resolve_agent` 会带 query_plan 一起路由，题集用 `plan_intent`
        # 声明"这句话被解析成哪个意图"，才能把 intent 分支也纳入回归
        # （早先只传 message，`routed_by` 永远不可能是 intent:*）。
        plan = {"intent": case["plan_intent"]} if case.get("plan_intent") else None
        decision = registry.route(case["message"], plan)
        manifest = registry.get(decision["agent_id"])
        tool_misses = tool_capability_misses(manifest, case)
        routed_ok = decision["agent_id"] == case["expect"]
        results.append({
            "id": case["id"],
            "message": case["message"],
            "plan_intent": case.get("plan_intent") or "",
            "expect": case["expect"],
            "actual": decision["agent_id"],
            "routed_by": decision["routed_by"],
            "tool_misses": tool_misses,
            "expect_tools": list(case.get("expect_tools") or []),
            "forbid_tools": list(case.get("forbid_tools") or []),
            "ok": routed_ok and not tool_misses,
        })
    accuracy = sum(1 for r in results if r["ok"]) / len(results) if results else 0.0
    return results, accuracy


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 路由准确率评测")
    parser.add_argument("--min-accuracy", type=float, default=0.0,
                        help="低于该准确率则以非 0 退出（CI 门槛）")
    parser.add_argument("--show-missed", action="store_true", help="只打印判错的用例")
    args = parser.parse_args()

    results, accuracy = evaluate()
    for item in results:
        if args.show_missed and item["ok"]:
            continue
        flag = "ok  " if item["ok"] else "MISS"
        intent = f"intent={item['plan_intent']:<10}" if item["plan_intent"] else ""
        print(f"  {flag} {item['id']:<4} expect={item['expect']:<13} actual={item['actual']:<13}"
              f" by={item['routed_by']:<22} {intent}{item['message'][:28]}")
        for miss in item["tool_misses"]:
            print(f"         - 能力缺失：{miss}")

    missed = [r for r in results if not r["ok"]]
    print(f"\n  路由准确率 {accuracy * 100:.1f}%（{len(results) - len(missed)}/{len(results)}）"
          "（含工具能力断言）")

    if args.min_accuracy and accuracy < args.min_accuracy:
        print(f"  ✗ 低于阈值 {args.min_accuracy * 100:.0f}%")
        return 1
    if args.min_accuracy:
        print(f"  ✓ 达标（阈值 {args.min_accuracy * 100:.0f}%）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
