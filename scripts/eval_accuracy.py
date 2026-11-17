"""演示评估脚本：量化筛选 vs Agent 研判 在标注集上的效果对比。

- 规则筛查：以 anomaly 表命中为「告警」，对比 eval_label 的 ground-truth；
- Agent 研判：抽样部分 SKU 走完整多 Agent 链路，用 risk_level（高/中=告警，低=不告警）对比
  expected_risk。

用法：
  python scripts/eval_accuracy.py                # 规则筛查 + 抽样 20 个 SKU 的 Agent 研判
  python scripts/eval_accuracy.py --sample 10    # 少抽样（省钱/省时）
  python scripts/eval_accuracy.py --rules-only   # 只跑规则筛查（不调用 LLM）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from app.db import repos  # noqa: E402
from app.db.engine import session_scope  # noqa: E402
from app.pipeline.patrol import inspect_sku  # noqa: E402


def _screening_confusion() -> dict:
    from app.pipeline.metrics import binary_confusion

    with session_scope() as session:
        labels = {
            label.sku_id: (label.anomaly_type, label.expected_risk)
            for label in repos.all_eval_labels(session)
        }
        flagged = {a.sku_id for a in repos.list_anomalies(session, limit=1000)}
    rows = []
    for sku_id, (atype, expected) in labels.items():
        truth = expected in ("高", "中") and atype != "normal"
        rows.append({"truth_pos": truth, "pred_pos": sku_id in flagged})
    stats = binary_confusion(rows)
    return stats


async def _agent_eval(sample: int) -> dict:
    from app.db.models import KbChunk
    from app.rag.index import build_index

    with session_scope() as session:
        if session.query(KbChunk).count() == 0:  # 若刚重建过库，先补知识库索引
            build_index(session)
        labels = {
            label.sku_id: (label.anomaly_type, label.expected_risk)
            for label in repos.all_eval_labels(session)
        }
        flagged = [a.sku_id for a in repos.list_anomalies(session, limit=1000)]
        unflagged = [s for s in labels if s not in set(flagged)]
    # 分层抽样：异常命中里均匀取，未命中里随机取 1/4
    step = max(1, len(flagged) // max(1, sample * 3 // 4))
    chosen = flagged[::step][: max(1, sample * 3 // 4)]
    rng = __import__("random").Random(7)
    chosen += rng.sample(unflagged, min(len(unflagged), sample - len(chosen)))

    sem = asyncio.Semaphore(3)
    rows: list[dict] = []

    async def _one(sku: str) -> None:
        async with sem:
            with session_scope() as session:
                run = repos.create_run(session, kind="single", params={"sku_id": sku, "scope": "eval"})
                run_id = run.id
            res = await inspect_sku(sku, run_id)
            final = res.get("final_state") or {}
            plan = final.get("plan") or {}
            with session_scope() as session:
                repos.finish_run(session, run_id, status=res["status"], stats={"scope": "eval"})
        label = labels[sku]
        atype, expected = label
        truth = expected in ("高", "中") and atype != "normal"
        pred = str(plan.get("risk_level") or "")
        # 复审 N7：跑失败/降级或没有预案时，**不能**用默认值补一个"中风险"当预测——
        # 那会把失败样本灌进混淆矩阵、把 precision 抬高。NULL = 未判定，单独计数。
        judged = res["status"] in {"ok", "degraded"} and bool(pred)
        rows.append({
            "sku": sku, "label": atype, "expected": expected,
            "pred": pred, "status": res["status"],
            "truth_pos": truth,
            "pred_pos": (pred in ("高", "中")) if judged else None,
            "issues": len((final.get("verification") or {}).get("issues") or []),
        })

    await asyncio.gather(*[_one(s) for s in chosen])

    from app.pipeline.metrics import binary_confusion

    stats = binary_confusion(rows)
    return {"sample": rows, **stats}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--rules-only", action="store_true")
    args = parser.parse_args()

    screening = _screening_confusion()
    print("=" * 72)
    print("规则筛查（多维度量化筛选）  ground-truth 标注集")
    print(f"  TP={screening['tp']:<3} FP={screening['fp']:<3} FN={screening['fn']:<3} TN={screening['tn']:<3}")
    print(f"  precision={screening['precision']:.1%}  recall={screening['recall']:.1%}  f1={screening['f1']:.1%}")

    if args.rules_only:
        return

    result = asyncio.run(_agent_eval(args.sample))
    print("-" * 72)
    print(f"Agent 研判（抽样 {len(result['sample'])} 个 SKU，风险等级 高/中 视为告警）")
    print(f"  TP={result['tp']:<3} FP={result['fp']:<3} FN={result['fn']:<3} TN={result['tn']:<3}"
          f"  可判定={result['judged']}  未判定(failed/无预案)={result['unjudged']}")
    print(f"  accuracy={result['accuracy']:.1%}  precision={result['precision']:.1%}  recall={result['recall']:.1%}")
    print("  （未判定的样本不计入矩阵：跑失败时预案为空，不能拿默认值当预测）")
    print()
    print(f"  {'SKU':<16}{'标注':<10}{'期望':<4}{'研判':<4}{'状态':<8}说明")
    for r in sorted(result["sample"],
                    key=lambda x: (x["pred_pos"] is not None, x["truth_pos"] != x["pred_pos"], x["sku"])):
        if r["pred_pos"] is None:
            flag = "?"
        else:
            flag = "✓" if r["truth_pos"] == r["pred_pos"] else "✗"
        print(f"  {r['sku']:<16}{r['label']:<10}{r['expected']:<4}"
              f"{(r['pred'] or '-'):<4}{r['status']:<8}校验问题 {r['issues']}  {flag}")
    print("=" * 72)


if __name__ == "__main__":
    main()
