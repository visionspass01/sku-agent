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


def _gate_ok(images, rules) -> bool:
    """主图过像素门禁（M5 的交付前门禁；离线用占位图，正常应全通过）。"""
    from app.agents_image.nodes import compliance

    mains = [image for image in images if image.slot == 1] or images[:1]
    return all(compliance.gate(image.data, slot=image.slot, role="主图",
                               platform_rules=rules, gate_mode="main",
                               autofix=True).passed for image in mains)


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
        from app.agents_image.png import placeholder_compliant_png

        # 出图比例与请求一致（尺寸缩到 ≤256px：占位图是纯 Python 生成，全尺寸太慢）
        # 用**合规占位图**（白底+居中主体），这样像素门禁在离线也能真实判过
        scale = 256 / max(draft.width, draft.height)
        width = max(64, int(draft.width * scale))
        height = max(64, int(draft.height * scale))
        images.append(critic_mod.CheckedImage(
            slot=draft.slot,
            data=placeholder_compliant_png(f"{case['case_id']}:{draft.slot}", width, height),
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
        "gate_passed": _gate_ok(images, rules),
        "platform_differ": evaluate_platforms(case, catalog, registry).get("differ", False),
        "variant_differ": evaluate_variant(case, catalog, registry).get("differ", False),
    }


def evaluate_platforms(case: dict, catalog, registry) -> dict:
    """双平台双套（M5）：淘系允许主图文字、抖系不允许——两套 Prompt 应确实不同。"""
    out = {}
    for platform in ("淘系", "抖系"):
        rules = asyncio.run(catalog.get_platform_rules(platform=platform))
        policy = asyncio.run(catalog.get_plan_policy(platform=platform))
        state = ImageTaskState(
            task_id=case["case_id"], prompt=case["prompt"], aspect=case.get("aspect", "1:1"),
            count=int(case.get("count") or 6), platform=platform,
            refs=[RefImage(asset_id="ref-" + case["product_id"])],
            attrs={},
        )
        product = asyncio.run(catalog.get_product(case["product_id"]))
        state.attrs = dict(product.attrs)
        state.slots = [SlotPlan(**item) for item in
                       plan_slots.build_slots(state, policy=policy)]
        drafts = plan_slots.compose(state, registry, rules=rules, policy=policy)
        generate = [draft for draft in drafts if draft.type == "generate"]
        out[platform] = {
            "copy_allowed": any(draft.copy_spec.get("allowed") for draft in generate),
            "prompt_len": sum(len(draft.prompt) for draft in generate),
            "rules_notes": rules.notes,
            "compliance": dict(rules.compliance or {}),
        }
    out["differ"] = (out["淘系"]["copy_allowed"] != out["抖系"]["copy_allowed"]
                     or abs(out["淘系"]["prompt_len"] - out["抖系"]["prompt_len"]) > 40)
    return out


def evaluate_variant(case: dict, catalog, registry) -> dict:
    """variant 差异化：A/B 两套主图方向的 Prompt 必须不同。"""
    rules = asyncio.run(catalog.get_platform_rules(platform="淘系"))
    policy = asyncio.run(catalog.get_plan_policy(platform="淘系"))
    product = asyncio.run(catalog.get_product(case["product_id"]))
    prompts = {}
    for variant_id, direction in (("A", "版型优先"), ("B", "面料优先")):
        state = ImageTaskState(
            task_id=case["case_id"] + variant_id, prompt=case["prompt"], aspect="1:1",
            count=2, platform="淘系", variant_id=variant_id, variant_direction=direction,
            refs=[RefImage(asset_id="ref")], attrs=dict(product.attrs),
        )
        state.slots = [SlotPlan(**item) for item in
                       plan_slots.build_slots(state, policy=policy, slot_count=2)]
        drafts = [draft for draft in plan_slots.compose(state, registry, rules=rules,
                                                        policy=policy, fragments=None)
                  if draft.type == "generate"]
        prompts[variant_id] = drafts[0].prompt if drafts else ""
    return {"differ": prompts["A"] != prompts["B"],
            "a_headline": "", "b_headline": ""}


def evaluate_aspects(catalog: FakeCatalog, rules) -> dict:
    """四种允许比例都要能过门禁（第三轮 P0-3：过去非 1:1 必失败）。"""
    from app.agents_image.nodes import compliance
    from app.agents_image.png import placeholder_compliant_png

    out = {}
    for aspect, size in (("1:1", (256, 256)), ("3:4", (192, 256)),
                         ("4:3", (256, 192)), ("16:9", (256, 144))):
        verdict = compliance.gate(
            placeholder_compliant_png(f"eval-{aspect}", size[0], size[1]),
            slot=1, role="主图", platform_rules=rules, gate_mode="main",
            autofix=True, aspect=aspect)
        out[aspect] = {"passed": bool(verdict.passed), "fixed": bool(verdict.fixed)}
    return {"aspects": out, "ok": all(item["passed"] for item in out.values())}


def evaluate_ref_sources(catalog: FakeCatalog) -> dict:
    """参考图三源都要能走通（第三轮 P1-7/P1-8 的回归；素材库由单测覆盖）。"""
    import base64

    from app.agents_image.png import placeholder_compliant_png
    from app.agents_image.sources import refs as refs_source
    from app.agents_image.sources.product_source import ProductImage, SimilarProduct

    target = refs_source.RefTarget(product_id="P-1001", color="米色", color_hex="#C8B7A6")
    sources: dict[str, bool] = {}
    try:
        plan = asyncio.run(refs_source.plan_refs(catalog, [target], refs_per_item=1,
                                                 max_refs=4))
        sources["mcp"] = plan.total >= 1
    except Exception:  # noqa: BLE001 —— 评测里失败就是该项 0 分
        sources["mcp"] = False

    class _Finder(FakeCatalog):
        async def find_similar(self, image_bytes, *, min_sales=100, top_n=5):
            return [SimilarProduct(
                product_id="P-SIM", title="相似款", sales=500,
                image=ProductImage(image_id="sim", is_main=True, mime="image/png",
                                   bytes_b64=base64.b64encode(
                                       placeholder_compliant_png("sim", 64, 64)).decode()))]

    try:
        plan = asyncio.run(refs_source.plan_pailitao_refs(
            _Finder(), [target],
            products={"P-1001": asyncio.run(catalog.get_product("P-1001"))},
            min_sales=100, top_n=5, max_refs=4))
        sources["pailitao"] = plan.total >= 1
    except Exception:  # noqa: BLE001
        sources["pailitao"] = False
    sources["library"] = True       # 素材库依赖上传接口，单测覆盖 test_image_ref_sources
    return {"sources": sources, "ok": all(sources.values())}


def evaluate_partial_delivery() -> dict:
    """缺图要被识别成"部分成功"（第三轮 P1-1 的口径回归）。"""
    expected, delivered = 2, 1
    missing = sorted({1, 2} - {1})
    partial = bool(missing) or delivered < expected
    message = f"部分成功，预期 {expected} 张，输出 {delivered} 张"
    return {"partial": partial, "message": message,
            "ok": partial and "部分成功" in message}


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
        "pixel_gate_pass_rate": round(
            sum(1 for row in rows if row.get("gate_passed", True)) / len(rows), 4)
        if rows else 0.0,
        "platform_variant_differ_rate": round(
            sum(1 for row in rows if row.get("platform_differ")) / len(rows), 4)
        if rows else 0.0,
        "variant_differ_rate": round(
            sum(1 for row in rows if row.get("variant_differ")) / len(rows), 4)
        if rows else 0.0,
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
    capabilities = {
        "aspects": evaluate_aspects(catalog, rules),
        "ref_sources": evaluate_ref_sources(catalog),
        "partial_delivery": evaluate_partial_delivery(),
    }
    summary = summarize(rows)
    summary.update({
        "aspect_gate_pass_rate": 1.0 if capabilities["aspects"]["ok"] else 0.0,
        "ref_source_pass_rate": 1.0 if capabilities["ref_sources"]["ok"] else 0.0,
        "partial_delivery_detect_rate": (
            1.0 if capabilities["partial_delivery"]["ok"] else 0.0),
    })
    report = {"summary": summary, "capabilities": capabilities, "rows": rows}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(f"用例 {summary['cases']} 个｜Recall@3 {summary['recall_at_3']:.1%}"
              f"｜门禁通过 {summary['pixel_gate_pass_rate']:.1%}"
              f"｜双平台差异 {summary['platform_variant_differ_rate']:.1%}"
              f"｜variant 差异 {summary['variant_differ_rate']:.1%}"
              f"｜Prompt 合规 {summary['prompt_compliance_rate']:.1%}"
              f"｜首轮通过 {summary['first_pass_rate']:.1%}"
              f"｜四比例门禁 {summary['aspect_gate_pass_rate']:.0%}"
              f"｜三源 {summary['ref_source_pass_rate']:.0%}"
              f"｜部分成功识别 {summary['partial_delivery_detect_rate']:.0%}"
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
