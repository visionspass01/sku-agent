"""RAG 检索评测 CLI（Recall@K / MRR / nDCG@5 / 无答案 / 消融）。

用法：
    python scripts/eval_rag.py                      # 当前配置（hash 向量 + 内存 RRF）
    python scripts/eval_rag.py --topk 5 --show-missed
    python scripts/eval_rag.py --negatives           # 加上负样本，评估「会不会硬答」
    python scripts/eval_rag.py --ablation            # bm25 / vector / rrf / rrf+rerank 对照表
    python scripts/eval_rag.py --conflicts           # 口径冲突题集（权威度裁决是否挑对）
    python scripts/eval_rag.py --min-recall 0.9 --min-no-answer 0.85   # CI 门槛
    python scripts/eval_rag.py --save                # 报告写 data/eval/report-<ts>.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from app.rag.eval import load_negatives, run_conflict_eval, run_eval  # noqa: E402

ABLATION = (
    ("bm25", "none"),
    ("vector", "none"),
    ("rrf", "none"),
    ("rrf", "heuristic"),
    ("rrf", "llm"),
)


def _print_report(report: dict, *, show_missed: bool) -> None:
    recall = report["recall_at"]
    print(f"题集 {report['positives']} 正样本 + {report['negatives']} 负样本 · "
          f"topk={report['topk']} · 融合={report.get('mode')} · 重排={report.get('rerank_backend')}")
    print(f"  Recall@1 = {recall['recall@1']:.1%}   Recall@3 = {recall['recall@3']:.1%}   "
          f"Recall@5 = {recall['recall@5']:.1%}")
    print(f"  MRR      = {report['mrr']:.3f}    nDCG@5   = {report['ndcg@5']:.3f}")
    rate = report.get("no_answer_rate")
    print(f"  无答案命中率 = {'-（未提供负样本）' if rate is None else f'{rate:.1%}'}")
    if report.get("by_department") and len(report["by_department"]) > 1:
        print("  分部门：")
        for name, item in report["by_department"].items():
            print(f"    {name:<8} n={item['count']:<3} R@1={item['recall@1']:.2f} "
                  f"R@5={item['recall@5']:.2f} MRR={item['mrr']:.3f}")
    if report.get("by_type") and len(report["by_type"]) > 1:
        print("  分类型：")
        for name, item in report["by_type"].items():
            print(f"    {name:<12} n={item['count']:<3} R@1={item['recall@1']:.2f} "
                  f"R@5={item['recall@5']:.2f}")
    if show_missed and report.get("missed"):
        print("  未命中明细：")
        for item in report["missed"]:
            print(f"    - {item['id']}: expected {item['expected']}, got {item['got']}")
    if report.get("false_answers"):
        print("  该弃答却硬答（负样本）：")
        for item in report["false_answers"]:
            print(f"    - {item['id']}: {item['query']} -> {item['got']}")


def _ablation() -> int:
    print("消融对比（同一题集，只改融合/重排）：")
    print(f"  {'融合':<8}{'重排':<10}{'R@1':>7}{'R@5':>7}{'MRR':>8}{'无答案':>8}{'降级':>6}")
    rows = []
    for mode, backend in ABLATION:
        try:
            report = run_eval(topk=5, mode=mode, rerank_backend=backend)
        except Exception as exc:  # noqa: BLE001 —— 可选后端缺失时不要中断整张表
            print(f"  {mode:<8}{backend:<10}  跳过（{type(exc).__name__}: {exc}）")
            continue
        rate = report.get("no_answer_rate")
        degraded = int(report.get("rerank_degraded") or 0)
        print(f"  {mode:<8}{backend:<10}{report['recall_at']['recall@1']:>7.1%}"
              f"{report['recall_at']['recall@5']:>7.1%}{report['mrr']:>8.3f}"
              f"{('-' if rate is None else f'{rate:.0%}'):>8}{degraded:>6}")
        rows.append({"mode": mode, "rerank": backend, "recall@1": report["recall_at"]["recall@1"],
                     "recall@5": report["recall_at"]["recall@5"], "mrr": report["mrr"],
                     "no_answer_rate": rate, "rerank_degraded": degraded})
    out = ROOT / "data" / "eval" / "ablation.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n降级 = 该后端超时/不可用而回退融合序的次数（llm/cross 需要网络或本地模型，离线会全降级）")
    print(f"对照表已写入 {out}")
    return 0


def _print_conflict_report(report: dict) -> None:
    print(f"口径冲突题集 {report['count']} 题 · 通过 {report['passed']} · "
          f"通过率 {report['pass_rate']:.1%}")
    for case in report["cases"]:
        mark = "OK  " if case["ok"] else "FAIL"
        print(f"  [{mark}] {case['id']}: status={case['status']} "
              f"conflicts={case['conflicts']}")
        for miss in case["misses"]:
            print(f"         - {miss}")


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 检索质量离线评测")
    parser.add_argument("--topk", type=int, default=5)
    parser.add_argument("--min-recall", type=float, default=0.0,
                        help="Recall@5 下限，低于则退出码 1（默认不设门槛）")
    parser.add_argument("--min-no-answer", type=float, default=0.0,
                        help="负样本「正确弃答」比例下限（需要 --negatives）")
    parser.add_argument("--negatives", action="store_true", help="加入负样本（应弃答）")
    parser.add_argument("--ablation", action="store_true", help="跑 bm25/vector/rrf/rerank 对照表")
    parser.add_argument("--conflicts", action="store_true",
                        help="跑口径冲突题集（谁能压过谁、有没有该报冲突却漏报）")
    parser.add_argument("--min-conflict", type=float, default=1.0,
                        help="冲突题通过率下限（配合 --conflicts，默认 100%%）")
    parser.add_argument("--mode", default="rrf", choices=["rrf", "bm25", "vector"])
    parser.add_argument("--rerank", default="none",
                        choices=["none", "heuristic", "llm", "cross"])
    parser.add_argument("--save", action="store_true", help="把报告写到 data/eval/report-<ts>.json")
    parser.add_argument("--show-missed", action="store_true", help="列出未命中的题目")
    args = parser.parse_args()

    if args.ablation:
        return _ablation()

    if args.conflicts:
        report = run_conflict_eval(topk=args.topk)
        _print_conflict_report(report)
        if report["pass_rate"] < args.min_conflict:
            print(f"\n[FAIL] 冲突题通过率 {report['pass_rate']:.3f} < 门槛 {args.min_conflict}")
            return 1
        print("\n[OK] 冲突题全部通过")
        return 0

    report = run_eval(
        topk=args.topk,
        negatives=load_negatives() if args.negatives else [],
        mode=args.mode,
        rerank_backend=args.rerank,
    )
    _print_report(report, show_missed=args.show_missed)

    if args.save:
        out_dir = ROOT / "data" / "eval"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"report-{ts}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {path}")

    code = 0
    if args.min_recall and report["recall_at"]["recall@5"] < args.min_recall:
        print(f"\n[FAIL] Recall@5={report['recall_at']['recall@5']:.3f} < 门槛 {args.min_recall}")
        code = 1
    rate = report.get("no_answer_rate")
    if args.min_no_answer and (rate is None or rate < args.min_no_answer):
        print(f"\n[FAIL] 无答案命中率={rate} < 门槛 {args.min_no_answer}（需要 --negatives）")
        code = 1
    if not code:
        print("\n[OK] 达到门槛")
    return code


if __name__ == "__main__":
    sys.exit(main())
