"""D16：离线评测与压测脚本的可执行性（不联网、不花 Key）。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_eval_cases_file_shape():
    lines = (ROOT / "data" / "eval" / "image_cases.jsonl").read_text(
        encoding="utf-8").splitlines()
    cases = [json.loads(line) for line in lines if line.strip()]
    assert len(cases) >= 8
    for case in cases:
        assert case["case_id"] and case["product_id"] and case["prompt"]
        assert isinstance(case["expect_docs"], list) and case["expect_docs"]


def test_eval_runs_offline_and_reports_metrics():
    module = _load("eval_image", ROOT / "scripts" / "eval_image.py")
    from app.agents_image.rag import index as rag_index

    rag_index.reset_index()
    cases = module.load_cases(2)
    catalog = module.FakeCatalog()
    rules = asyncio.run(catalog.get_platform_rules(platform="淘系"))
    rows = [module.evaluate_case(case, catalog, module.get_registry(), rules)
            for case in cases]
    summary = module.summarize(rows)
    assert summary["cases"] == 2
    assert 0.0 <= summary["recall_at_3"] <= 1.0
    assert 0.0 <= summary["prompt_compliance_rate"] <= 1.0
    assert summary["elapsed_ms_p50"] >= 0
    assert all(row["slots"] >= row["generate_slots"] for row in rows)
    # 首轮通过率：占位图比例正确，Critic 应该全部通过
    assert summary["first_pass_rate"] == 1.0


def test_bench_runs_with_small_images():
    module = _load("bench_image", ROOT / "scripts" / "bench_image.py")
    result = asyncio.run(module._run(4, 2, 96))
    assert result["tasks"] == 4
    assert result["throughput_per_second"] > 0
    assert result["latency_ms_p50"] >= 0
    assert result["cost_cny"] == 0.0            # fake provider 零成本


def test_eval_and_bench_have_no_network_imports():
    for name in ("eval_image.py", "bench_image.py", "check_image.py"):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "requests" not in source
        assert "httpx" not in source            # 评测/压测/校验都只跑本地链路
