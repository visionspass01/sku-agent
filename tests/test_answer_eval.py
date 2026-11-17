"""答案质量判定器测试：维度判定正确、聚合分母正确、脚本离线自检可跑。"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

CASE = {
    "id": "t1",
    "message": "ST-01-C1-M 可用库存？",
    "expect": {"tools": ["get_inventory"], "must_mention": ["可用库存"], "no_write": True},
}
TRACE = [
    {"type": "call", "tool": "get_inventory", "args": {"sku_id": "ST-01-C1-M"}},
    {"type": "result", "tool": "get_inventory", "result": '{"available": 693}'},
]
STEPS = [{"tool": "get_inventory", "result_id": "res_4db97eeeb5"}]


def test_judge_passes_good_answer():
    from app.agents.answer_eval import judge

    verdict = judge(CASE, "可用库存 693 件（res_4db97eeeb5）。", TRACE, STEPS)
    assert verdict["passed"] is True
    assert verdict["label"] == "pass"


def test_judge_flags_missing_tool():
    from app.agents.answer_eval import judge

    verdict = judge(CASE, "可用库存 700 件左右。", [], [])
    assert verdict["passed"] is False
    assert any("未调用期望工具" in i for i in verdict["issues"])


def test_judge_flags_fabricated_citation():
    from app.agents.answer_eval import judge

    verdict = judge(CASE, "可用库存 693 件（res_ffffffffff）。", TRACE, STEPS)
    assert verdict["passed"] is False
    assert any("不存在的来源" in i for i in verdict["issues"])


def test_judge_flags_write_in_readonly_case():
    from app.agents.answer_eval import judge

    trace = TRACE + [
        {"type": "call", "tool": "create_replenishment_order", "args": {"qty": 500}},
        {"type": "result", "tool": "create_replenishment_order", "result": '{"status": "pending_confirmation"}'},
    ]
    verdict = judge(CASE, "可用库存 693 件（res_4db97eeeb5），已创建补货草稿单。", trace, STEPS)
    assert verdict["passed"] is False
    assert any("写工具" in i for i in verdict["issues"])


def test_judge_forbid_mention():
    from app.agents.answer_eval import judge

    case = {**CASE, "expect": {**CASE["expect"], "forbid_mention": ["已审批通过"]}}
    verdict = judge(case, "可用库存 693 件（res_4db97eeeb5），补货单已审批通过。", TRACE, STEPS)
    assert verdict["passed"] is False
    assert any("不该出现" in i for i in verdict["issues"])


def test_judge_allows_derived_numbers_when_opted_in():
    """模型算出来的派生数字（建议补货量）不该被判成幻觉，用例可显式放开。"""
    from app.agents.answer_eval import judge

    case = {
        "id": "t2", "message": "要补多少？",
        "expect": {"tools": ["get_inventory"], "allow_unverified_numbers": True},
    }
    verdict = judge(case, "建议补货 120 件（res_4db97eeeb5）。", TRACE, STEPS)
    assert verdict["checks"]["grounding"] is True
    assert verdict["passed"] is True


def test_aggregate_denominators_not_inflated():
    """没检查某一维度时不该计入该维度分母（否则通过率会 >100%）。"""
    from app.agents.answer_eval import aggregate

    results = [
        {"passed": True, "checks": {"tools": True, "no_write": True}, "issues": []},
        {"passed": True, "checks": {"tools": True}, "issues": []},
    ]
    agg = aggregate(results)
    assert agg["dims"]["no_write"] == {"checked": 1, "passed": 1, "rate": 1.0}
    assert agg["dims"]["tools"]["checked"] == 2
    assert agg["pass_rate"] == 1.0


def test_unchecked_dimension_rate_is_none_not_one():
    """复审 A7：没检查 ≠ 通过。`rate=1.0` 会让"只被少数用例覆盖的维度"看起来满格。"""
    from app.agents.answer_eval import aggregate

    agg = aggregate([{"passed": True, "checks": {"tools": True}, "issues": []}])
    assert agg["dims"]["no_write"]["checked"] == 0
    assert agg["dims"]["no_write"]["rate"] is None
    assert agg["dims"]["tools"]["rate"] == 1.0


def test_offline_eval_script_reports_consistent_labels():
    """脚本离线自检：判定器对人工样例的结论必须与标签一致（CI 门槛）。"""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1"}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "eval_agent_answers.py"), "--offline"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), env=env, timeout=180,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-1000:]
    assert "判定器自检通过" in proc.stdout


def test_dataset_is_valid_jsonl():
    path = ROOT / "data" / "eval" / "agent_answers.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) >= 8
    ids = {r["id"] for r in rows}
    assert len(ids) == len(rows)
    for row in rows:
        assert row["message"] and "expect" in row


def test_write_tool_names_derive_from_platform_registry():
    """复审 P1：评测盯的写工具必须与装配侧同一份名单，且不能出现虚构工具名。

    早先这里手抄了 `create_after_sales_proposal` / `approve` / `reject`（都不存在），
    却漏掉真实写工具 `create_after_sales` 与 `save_analysis` —— 只读用例因此会假通过。
    """
    from app.agents.answer_eval import SUGGESTION_TOOL_NAMES, WRITE_TOOLS
    from app.tools.platform_tools import ALL_TOOL_NAMES, WRITE_TOOL_NAMES

    assert set(WRITE_TOOL_NAMES) <= WRITE_TOOLS, "真实写工具一个都不能漏"
    assert SUGGESTION_TOOL_NAMES <= WRITE_TOOLS
    assert WRITE_TOOLS <= set(ALL_TOOL_NAMES), sorted(WRITE_TOOLS - set(ALL_TOOL_NAMES))


def test_judge_flags_real_write_tool_call():
    """复审 P1：真实写工具（建单/售后/沉淀档案）必须被判成误触。"""
    from app.agents.answer_eval import judge
    from app.tools.platform_tools import WRITE_TOOL_NAMES

    for tool in sorted(WRITE_TOOL_NAMES):
        trace = TRACE + [
            {"type": "call", "tool": tool, "args": {"order_id": "SO1", "qty": 10}},
            {"type": "result", "tool": tool, "result": '{"status": "pending_confirmation"}'},
        ]
        verdict = judge(CASE, "可用库存 693 件（res_4db97eeeb5）。", trace, STEPS)
        assert verdict["passed"] is False, tool
        assert any("写工具" in i for i in verdict["issues"]), (tool, verdict["issues"])


def test_dataset_uses_real_tool_names():
    """题集里的工具名必须真实存在（早先 q05 用了虚构的 create_after_sales_proposal）。"""
    from app.tools.platform_tools import ALL_TOOL_NAMES

    path = ROOT / "data" / "eval" / "agent_answers.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    used: set[str] = set()
    for row in rows:
        sample = row.get("sample") or {}
        for item in sample.get("tool_trace") or []:
            if item.get("type") == "call" and item.get("tool"):
                used.add(str(item["tool"]))
            elif item.get("tool"):
                used.add(str(item["tool"]))
        for item in sample.get("steps") or []:
            if item.get("tool"):
                used.add(str(item["tool"]))
        for item in (row.get("expect") or {}).get("tools") or []:
            used.add(str(item))
    assert used, "题集里没有工具名？"
    assert used <= set(ALL_TOOL_NAMES), sorted(used - set(ALL_TOOL_NAMES))


def _eval_script():
    import importlib.util

    path = ROOT / "scripts" / "eval_agent_answers.py"
    spec = importlib.util.spec_from_file_location("eval_agent_answers_mod", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_and_judge_sample_kinds_are_partitioned():
    """复审 P3：`--live` 只跑 kind=live；「（反例）」自检样例只进离线自检。"""
    module = _eval_script()
    live = module.load_dataset(kind="live")
    samples = module.load_dataset(kind="judge_sample")
    assert live and samples
    assert all("（反例）" not in case["message"] for case in live)
    assert {case["id"] for case in live}.isdisjoint({case["id"] for case in samples})
    # 离线自检要拿到全部人工样例（含 live 里带 sample 的）
    with_sample = module.load_dataset(with_sample=True)
    assert {case["id"] for case in samples} <= {case["id"] for case in with_sample}
    assert all((case.get("sample") or {}).get("expect_label") for case in with_sample)


def test_llm_judge_parses_verdict(monkeypatch):
    """LLM-as-Judge：模型返回结构化判定 → 归一成 checked/faithful/score。"""
    import asyncio

    from app.agents import answer_eval

    class _LLM:
        async def invoke_json(self, system, user, schema, max_tokens=None):
            assert "工具结果" in user and "待判定答案" in user
            return schema(faithful=False, score=0.2, issues=["库存数量无依据"], reason="工具结果里没有该数字")

    monkeypatch.setattr("app.llm.get_llm", lambda: _LLM())
    out = asyncio.run(answer_eval.llm_judge(
        {"message": "库存多少？"}, "库存 999 件。", TRACE,
    ))
    assert out["checked"] is True
    assert out["faithful"] is False
    assert out["score"] == 0.2
    assert out["issues"]


def test_llm_judge_degrades_when_model_unavailable(monkeypatch):
    """模型不可用不能让整轮评测失败，只标记 checked=false。"""
    import asyncio

    from app.agents import answer_eval
    from app.llm import LLMError

    class _LLM:
        async def invoke_json(self, *args, **kwargs):
            raise LLMError("未配置 DEEPSEEK_API_KEY")

    monkeypatch.setattr("app.llm.get_llm", lambda: _LLM())
    out = asyncio.run(answer_eval.llm_judge({"message": "q"}, "a", TRACE))
    assert out["checked"] is False and out["faithful"] is None
    assert "不可用" in out["note"]


def test_aggregate_with_judge_counts_only_judged():
    from app.agents.answer_eval import aggregate_with_judge

    results = [
        {"passed": True, "checks": {"tools": True}, "issues": [],
         "llm_judge": {"checked": True, "faithful": True, "score": 0.9}},
        {"passed": False, "checks": {"tools": False}, "issues": ["x"],
         "llm_judge": {"checked": True, "faithful": False, "score": 0.3}},
        {"passed": True, "checks": {"tools": True}, "issues": [],
         "llm_judge": {"checked": False, "faithful": None, "score": None}},
    ]
    agg = aggregate_with_judge(results)
    assert agg["llm_judge"] == {"checked": 2, "faithful": 1, "avg_score": 0.6}
    assert agg["pass_rate"] == 0.6667


def test_eval_script_accepts_judge_flag():
    """--judge 必须是真实参数（文档里写了就得能跑）。"""
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1"}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "eval_agent_answers.py"), "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), env=env, timeout=120,
    )
    assert proc.returncode == 0
    assert "--judge" in proc.stdout and "rule" in proc.stdout and "llm" in proc.stdout
