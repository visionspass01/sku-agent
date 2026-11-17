"""离线评测（对应计划 §21）：召回 / Prompt 合规 / Critic 首轮通过 / 时延，全程不联网。

用法：
    PYTHONPATH=src python scripts/eval_image.py              # 全量用例
    PYTHONPATH=src python scripts/eval_image.py --limit 3 --json
    PYTHONPATH=src python scripts/eval_image.py --out data/eval/report-image.json

口径：
- **Recall@k**：用例 `expect_docs` 里有多少命中了检索 top-k（拼接所有图位的查询）；
- **Prompt 合规率**：七条铁律在 Prompt 里可验证的部分（hex 颜色、占比/留白数字、
  否定清单含 must_not_change、主图角标预留、图内文字策略）；
- **首轮通过率**：用 FakeProvider 出图 → Critic 一次判定的通过比例；
- **时延**：规划 + 召回 + Prompt + 评审的单任务耗时（P50/P95）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.agents_image.nodes import critic as critic_mod  # noqa: E402
from app.agents_image.nodes import plan_slots  # noqa: E402
from app.agents_image.rag import index as rag_index  # noqa: E402
from app.agents_image.registry import get_registry  # noqa: E402
from app.agents_image.sources.fake_catalog import FakeCatalog  # noqa: E402
from app.agents_image.state import ImageTaskState, RefImage, SlotPlan  # noqa: E402

CASES = ROOT / "data" / "eval" / "image_cases.jsonl"


def load_cases(limit: int | None = None) -> list[dict]:
    cases: list[dict] = []
    for line in CASES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases[:limit] if limit else cases


def _prompt_compliance(drafts) -> bool:
    """七条铁律里可机检的部分（缺一条就算不合规，便于回归）。"""
    if not drafts:
        return False
    first = drafts[0]
    prompt = first.prompt
    return all([
        "#" in prompt,                                  # ① hex 颜色
        "%" in prompt,                                  # ②③ 占比/留白数字
        "保持以下不变" in prompt or "不变" in prompt,     # ④ 否定清单
        first.preserve,                                 # ④ 身份锁定字段
        "平台角标" in prompt or first.slot != 1,          # ⑤ 主图预留
        first.text_policy in {"none", "headline", "spec"},  # ⑥ 图内文字策略
        "图位" in prompt,                                # ⑦ 单图位隔离
    ])


def evaluate_case(case: dict, catalog: FakeCatalog, registry, rules) -> dict:
    started = time.perf_counter()
    product = asyncio.run(catalog.get_product(case["product_id"]))
    policy = asyncio.run(catalog.get_plan_policy())
    state = ImageTaskState(
        task_id=case["case_id"], prompt=case["prompt"], aspect=case.get("aspect", "1:1"),
        count=int(case.get("count") or 6), platform="淘系",
        refs=[RefImage(asset_id="ref-" + case["product_id"], product_id=case["product_id"])],
        attrs=dict(product.attrs),
    )
    state.slots = [SlotPlan(**item) for item in plan_slots.build_slots(state, policy=policy)]

    # 召回：按图位拼查询，取 top-3 并集
    retrieved: list[str] = []
    for item in state.slots:
        query = " ".join(part for part in (
            str(state.attrs.get("product_type") or ""), item.role,
            item.composition_label, item.scene_style_label, state.prompt) if part)
        for hit in rag_index.retrieve(query, top_k=3):
            if hit.doc.doc_id not in retrieved:
                retrieved.append(hit.doc.doc_id)
    expected = set(case.get("expect_docs") or [])
    recall_hit = len(expected & set(retrieved)) / len(expected) if expected else 1.0

    drafts = plan_slots.compose(state, registry, rules=rules)
    generate = [draft for draft in drafts if draft.type == "generate"]
    images = []
    for draft in generate:
        from app.agents_image.png import placeholder_png

        # 出图比例与请求一致（尺寸缩到 ≤256px：占位图是纯 Python 生成，全尺寸太慢）
        scale = 256 / max(draft.width, draft.height)
        width = max(64, int(draft.width * scale))
        height = max(64, int(draft.height * scale))
        images.append(critic_mod.CheckedImage(
            slot=draft.slot,
            data=placeholder_png(f"{case['case_id']}:{draft.slot}", width, height),
            mime="image/png", width=width, height=height,
        ))
    critic = critic_mod.critique(state, generate, images, rules=rules)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "case_id": case["case_id"], "product_id": case["product_id"],
        "slots": len(drafts), "generate_slots": len(generate),
        "recall": round(recall_hit, 3), "retrieved": retrieved,
        "prompt_compliant": _prompt_compliance(drafts),
        "critic_passed": critic.passed, "critic_score": critic.score,
        "issues": critic.issues, "elapsed_ms": elapsed_ms,
    }


def summarize(rows: list[dict]) -> dict:
    times = [row["elapsed_ms"] for row in rows] or [0]
    passed = sum(1 for row in rows if row["critic_passed"])
    compliant = sum(1 for row in rows if row["prompt_compliant"])
    recall = statistics.fmean(row["recall"] for row in rows) if rows else 0.0
    ordered = sorted(times)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return {
        "cases": len(rows),
        "recall_at_3": round(recall, 4),
        "prompt_compliance_rate": round(compliant / len(rows), 4) if rows else 0.0,
        "first_pass_rate": round(passed / len(rows), 4) if rows else 0.0,
        "elapsed_ms_p50": int(statistics.median(times)),
        "elapsed_ms_p95": int(p95),
        "rag_cache": rag_index.cache_stats(),
        "index": rag_index.index_status(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生图 Agent 离线评测（不联网、不花 Key）")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    rag_index.reset_index()
    catalog = FakeCatalog()
    rules = asyncio.run(catalog.get_platform_rules(platform="淘系"))
    registry = get_registry()
    cases = load_cases(args.limit or None)
    rows = [evaluate_case(case, catalog, registry, rules) for case in cases]
    report = {"summary": summarize(rows), "rows": rows}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(f"用例 {summary['cases']} 个｜Recall@3 {summary['recall_at_3']:.1%}"
              f"｜Prompt 合规 {summary['prompt_compliance_rate']:.1%}"
              f"｜首轮通过 {summary['first_pass_rate']:.1%}"
              f"｜P50 {summary['elapsed_ms_p50']}ms / P95 {summary['elapsed_ms_p95']}ms")
        for row in rows:
            mark = "OK" if row["critic_passed"] and row["prompt_compliant"] else "NG"
            print(f"  [{mark}] {row['case_id']} recall={row['recall']:.2f} "
                  f"slots={row['slots']}/{row['generate_slots']} critic={row['critic_score']}")
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {target}")
    return 0 if all(row["critic_passed"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
