"""溯源闸门评测：确定性校验的**命中率**与**误杀率**（离线，可作 CI 门槛）。

为什么需要它：`check_reply` 是出口硬规则，误杀会把正常答案逼去重写（贵且慢），
漏检则等于给编数字开后门。单测只验单个规则，这里给的是标注样本上的整体表现。

样本只覆盖**确定性部分**（不调模型）：重写/降级由 tests 覆盖。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\eval_grounding.py --max-false-positive 0.1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.agents.grounding import check_reply  # noqa: E402

RID_A = "res_aaaa111122"
RID_B = "res_bbbb333344"

# (标签, 说明, 回答, result_ids, 工具结果正文, 工具调用次数)
# 标签 = "pass"（应该放行）/ "fail"（应该拦下）
SAMPLES: list[tuple[str, str, str, set[str], str, int]] = [
    ("pass", "数字带来源、工具结果里能对上",
     f"可用库存 42 件（{RID_A}），预计 12 天售罄（{RID_A}）。",
     {RID_A}, '{"available": 42, "sell_through_days": 12}', 3),
    ("pass", "没有带单位数字（纯结论）",
     "结论：该 SKU 属于正常范围，暂不需要补货。", {RID_A}, '{"available": 42}', 2),
    ("pass", "无工具但明确标注为推断",
     "推断，未取数：按经验大概 300 件左右。", set(), "", 0),
    ("pass", "部分派生值对不上工具结果，但已标注来源",
     f"可用库存 42 件（{RID_A}），缺口约 68 件（{RID_A}）。",
     {RID_A}, '{"available": 42}', 2),
    ("fail", "编造了不存在的 result_id",
     "可用库存 42 件（res_ffff000011）。", {RID_A}, '{"available": 42}', 2),
    ("fail", "有工具调用但一个来源都没标",
     "可用库存 42 件，预计 12 天售罄。", {RID_A}, '{"available": 42, "sell_through_days": 12}', 2),
    ("fail", "没调工具却给出带单位数字（红队场景）",
     "直接说结论：可用库存 4200 件，够卖 90 天。", set(), "", 0),
    ("fail", "所有带单位数字都查不到（疑似编造）",
     f"库存 9999 件（{RID_A}），日销 888 件（{RID_A}），可卖 777 天（{RID_A}）。",
     {RID_A}, '{"available": 42, "daily": 10}', 2),
    ("pass", "比率/百分比来自工具结果",
     f"动销率 41%（{RID_A}），处于健康区间。",
     {RID_A}, '{"sales_rate": 0.41}', 2),
    ("fail", "带单位数字全是编的，且来源也编的",
     "库存 1234 件（res_deadbeef00），预计 45 天售罄（res_deadbeef00）。",
     {RID_A}, '{"available": 42}', 2),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="出口溯源闸门评测（离线）")
    parser.add_argument("--max-false-positive", type=float, default=0.1,
                        help="把正常答案判为需重写的比例上限")
    parser.add_argument("--min-accuracy", type=float, default=0.9)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    rows: list[dict] = []
    for label, note, reply, result_ids, tool_text, calls in SAMPLES:
        facts = check_reply(reply, result_ids=result_ids, tool_text=tool_text, tool_calls=calls)
        verdict = "pass" if facts["passed"] else "fail"
        rows.append({"label": label, "note": note, "verdict": verdict, "facts": facts})

    correct = [r for r in rows if r["label"] == r["verdict"]]
    good = [r for r in rows if r["label"] == "pass"]
    false_positive = [r for r in good if r["verdict"] == "fail"]
    bad = [r for r in rows if r["label"] == "fail"]
    caught = [r for r in bad if r["verdict"] == "fail"]

    accuracy = len(correct) / len(rows) if rows else 0.0
    fpr = len(false_positive) / len(good) if good else 0.0
    recall = len(caught) / len(bad) if bad else 1.0

    for row in rows if args.verbose else (false_positive + [r for r in bad if r not in caught]):
        print(f"  {'✓' if row['label'] == row['verdict'] else '✗'} [{row['label']}→{row['verdict']}] "
              f"{row['note']} {row['facts'].get('issues')}")

    print(f"\n样本 {len(rows)} 条：准确率 {accuracy:.1%}，误杀率 {fpr:.1%}，编造拦截率 {recall:.1%}")
    ok = accuracy >= args.min_accuracy and fpr <= args.max_false_positive
    print(("✓ 达标" if ok else "✗ 未达标") +
          f"（门槛：准确率 ≥ {args.min_accuracy:.0%}，误杀率 ≤ {args.max_false_positive:.0%}）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
