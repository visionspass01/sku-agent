"""Trajectory 指标评测（E6）：每轮工具数 / 步数 / 重复调用率 / 预算利用率 / 循环中止率 / 多轮成功率。

用法：
    PYTHONPATH=src python scripts/eval_trajectory.py --from-db                # 从演示库的 chat_turn_log 聚合
    PYTHONPATH=src python scripts/eval_trajectory.py --from-db --hours 72     # 换窗口
    PYTHONPATH=src python scripts/eval_trajectory.py --live --limit 3         # 真跑 Agent（需 API Key）
    PYTHONPATH=src python scripts/eval_trajectory.py --from-db --max-repeat-rate 0.2  # 当门槛用

为什么单独评这一层：
- `eval_routing` 测「派给谁」、`eval_rag` 测「找不找得到」、`eval_agent_answers` 测「答得对不对」；
- 但一个 Agent 变差往往先表现在**轨迹**上：同一工具反复调、工具数爆掉、预算打满、追问重来一遍。
  这些指标纯统计（零 token），可以在真模型之外单独回归。

口径（与 `app/agents/trajectory.py` 一致）：
- `repeat_calls` 用 `steps[].args`（200 字截断串）做键 —— 代理指标；
- `aborted_turns` 只统计已持久化的循环/预算中止（`grounding_status=aborted`）；
- `followup_success_rate` 是**流程成功率**（grounding 通过且未中止），不是业务正确率。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# live 模式用的问题集（覆盖"纯取数""解释类""多 SKU"三种轨迹形态）
LIVE_QUESTIONS = [
    "ST-01-C1-M 当前可用库存多少",
    "可用库存和现存库存有什么区别",
    "ST-01-C1-M、ST-02-C1-M、ST-03-C1-M 的可用库存分别是多少",
]


def _db_rows(hours: int) -> list[dict]:
    from app.agents.chat_store import load_turn_rows
    from app.db.engine import session_scope

    with session_scope() as session:
        return load_turn_rows(session, hours=hours)


def _live_rows(limit: int) -> list[dict]:
    from app.agents.chat_agent import run_chat
    from app.auth import AuthUser
    from app.memory.runtime import chat_turn_context

    user = AuthUser(username="admin", role="admin", department=None)
    rows: list[dict] = []
    for question in LIVE_QUESTIONS[:max(1, limit)]:
        with chat_turn_context(session_id=None, include_query_plan=False):
            result = asyncio.run(run_chat(user, [], question))
        detail = result.get("run_detail") or {}
        rows.append({
            "session_id": "",
            "agent_id": str((detail.get("agent") or {}).get("id") or ""),
            "grounding_status": str((detail.get("grounding") or {}).get("status") or ""),
            "payload": detail,
            "created_at": "",
        })
    return rows


def _print_report(data: dict) -> None:
    print(f"trajectory 窗口 {data['window_hours']}h：{data['turns']} 轮"
          f"（中止 {data['aborted_turns']} 轮，{data['abort_rate'] * 100:.1f}%）")
    tools = data["tool_calls_per_turn"]
    llm = data["llm_calls_per_turn"]
    print(f"  工具/轮  avg={tools['avg']}  p50={tools['p50']}  p95={tools['p95']}  max={tools['max']}")
    print(f"  模型/轮  avg={llm['avg']}  p50={llm['p50']}  p95={llm['p95']}  max={llm['max']}")
    print(f"  重复调用 {data['repeat_calls']} 次（占工具调用 {data['repeat_call_rate'] * 100:.1f}%，"
          f"{data['turns_with_repeats']} 轮出现过）")
    if data["turns_with_guard"]:
        print(f"  预算利用率 avg={data['budget_utilization'] * 100:.1f}%"
              f"（{data['turns_with_guard']} 轮带 guard 数据）")
    if data["error_codes"]:
        print(f"  中止错误码：{data['error_codes']}")
    multi = data["multi_turn"]
    rate = multi["followup_success_rate"]
    rate_text = "无可用判定（缺 grounding 状态）" if rate is None else f"{rate * 100:.1f}%"
    print(f"  多轮：{multi['sessions']} 个会话 / {multi['followup_turns']} 个追问"
          f"（可判定 {multi['followup_judged']}），追问流程成功率 {rate_text}")
    if data["top_tools"]:
        top = "、".join(f"{item['tool']}×{item['calls']}" for item in data["top_tools"][:5])
        print(f"  高频工具：{top}")
    if data["per_agent"]:
        print("  按 Agent：")
        for agent_id, stat in data["per_agent"].items():
            print(f"    {agent_id:<14} turns={stat['turns']:<4} avg_tools={stat['avg_tools']:<6}"
                  f" p95_tools={stat['p95_tools']:<6} repeat={stat['repeat_call_rate'] * 100:.1f}%"
                  f" abort={stat['abort_rate'] * 100:.1f}%")
    print(f"  本轮窗口成本估算 ¥{data['cost_cny']}")
    print(f"  note：{data['note']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Trajectory 指标评测（工具/步数/重复/预算/中止/多轮）")
    parser.add_argument("--from-db", action="store_true", help="从 chat_turn_log 聚合（默认）")
    parser.add_argument("--live", action="store_true", help="真跑 Agent 采集（需要 DEEPSEEK_API_KEY）")
    parser.add_argument("--hours", type=int, default=24, help="窗口小时数（from-db 用）")
    parser.add_argument("--limit", type=int, default=3, help="live 模式跑几条问题")
    parser.add_argument("--min-turns", type=int, default=1,
                        help="窗口内至少多少轮才判定（默认 1：没数据就报错，避免「0 轮也达标」）")
    parser.add_argument("--max-avg-tools", type=float, default=15.0, help="平均每轮工具调用上限")
    parser.add_argument("--max-p95-tools", type=float, default=20.0, help="每轮工具调用 P95 上限")
    parser.add_argument("--max-repeat-rate", type=float, default=0.3, help="重复调用占比上限")
    parser.add_argument("--max-abort-rate", type=float, default=0.2, help="循环/预算中止率上限")
    parser.add_argument("--json-out", default="", help="把聚合结果写到该 JSON 文件")
    args = parser.parse_args()

    from app.agents import trajectory

    rows = _live_rows(args.limit) if args.live else _db_rows(args.hours)
    data = trajectory.aggregate(rows, hours=args.hours)
    _print_report(data)

    if args.json_out:
        pathlib.Path(args.json_out).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  明细已写入 {args.json_out}")

    breaches: list[str] = []
    if data["turns"] < args.min_turns:
        breaches.append(f"轮次不足：{data['turns']} < {args.min_turns}")
    if data["tool_calls_per_turn"]["avg"] > args.max_avg_tools:
        breaches.append(f"平均工具数 {data['tool_calls_per_turn']['avg']} > {args.max_avg_tools}")
    if data["tool_calls_per_turn"]["p95"] > args.max_p95_tools:
        breaches.append(f"P95 工具数 {data['tool_calls_per_turn']['p95']} > {args.max_p95_tools}")
    if data["repeat_call_rate"] > args.max_repeat_rate:
        breaches.append(f"重复调用率 {data['repeat_call_rate']:.2%} > {args.max_repeat_rate:.2%}")
    if data["abort_rate"] > args.max_abort_rate:
        breaches.append(f"中止率 {data['abort_rate']:.2%} > {args.max_abort_rate:.2%}")

    if breaches:
        print("\n  ✗ 未达标：")
        for item in breaches:
            print(f"    - {item}")
        return 1
    print("\n  ✓ 达标")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
