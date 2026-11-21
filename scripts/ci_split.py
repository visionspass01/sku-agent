"""把用例按**项目功能**分组：CI 的每一步验一个功能面，而不是"跑一遍全部用例"。

为什么要有这个文件：

- 之前 CI 只有 `pytest tests/test_*.py` 一条命令，红了只知道"某个地方挂了"；
- 现在按 `src/` 的结构切成若干功能面（中台 / Agent 图 / RAG / 记忆 / 基础设施 / 生图管线…），
  每一步单独成 step，失败时 Actions 页面上直接看到「哪一面挂了」；
- 分组只在这里定义一次：`.github/workflows/gate.yml` 与本地镜像 `scripts/ci_local.py`
  都调它（`--files sku:rag`），避免两边各抄一份文件清单再漂移。

用法：
    python scripts/ci_split.py --list                  # 列出所有分组与文件数
    python scripts/ci_split.py --files sku:rag         # 打印该组的用例路径（空格分隔）
    python scripts/ci_split.py --verify                # 校验：每个用例恰好属于一组（无遗漏/无重叠）
    python scripts/ci_split.py --verify --strict       # 额外要求 misc 组为空（新用例必须归类）

规则匹配用 fnmatch，**按顺序**first-match-wins，所以更具体的名字写在前面。
"""

from __future__ import annotations

import argparse
import fnmatch
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# (slug, 中文标题, 匹配模式) —— 顺序即 CI 里的执行顺序：从边界到核心
SKU_GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    ("platform", "中台·身份/作用域/契约/治理", (
        "test_platform_governance.py", "test_platform_authz.py", "test_platform_contract.py",
        "test_platform_client_headers.py", "test_platform_orders.py",
        "test_platform_replenish_idem.py", "test_platform.py",
        "test_auth.py", "test_followup_authz.py", "test_tool_scope.py",
        "test_accounts_shared.py", "test_kb_scope.py", "test_org_memory_scope.py",
        "test_order_scope_cache.py", "test_order_server.py",
        "test_agent_registration.py", "test_agent_registry.py",
        "test_agent_manifest_hardening.py", "test_capabilities_sync.py",
        "test_seed_fk_enforced.py", "test_deploy_guards.py",
        "test_after_sales_approval.py", "test_tool_idempotency.py",
        "test_concurrency_limits.py",
    )),
    ("data", "中台·指标口径/数据质量/巡检/可观测", (
        "test_metrics*.py", "test_demo_catalog_contract.py", "test_apparel_tools.py",
        "test_stock_source_semantics.py", "test_screening.py",
        "test_patrol.py", "test_patrol_lock.py",
        "test_observability.py", "test_agent_observability.py", "test_r5_observability.py",
        "test_otel.py", "test_trace_propagation.py",
    )),
    ("graph", "Agent·图/路由/技能/工具面/checkpoint", (
        "test_graph*.py", "test_agent_routing.py", "test_agent_tools.py",
        "test_agent_tool_surface.py", "test_agent_catalog.py",
        "test_skills.py", "test_skill_docs.py",
        "test_prompts.py", "test_prompt_hot_reload.py",
        "test_plan_schema.py", "test_query_plan.py",
        "test_react_loop*.py", "test_tool_selection.py", "test_tool_guard.py",
        "test_tool_latency.py", "test_tool_stats.py",
        "test_unknown_tool_recovery.py", "test_handoff.py", "test_checkpoint*.py",
    )),
    ("analysis", "Agent·取数分析/档案/前端页面", (
        "test_analysis*.py", "test_api.py", "test_ui_pages.py", "test_ui_adjustments.py",
        "test_user_profile.py", "test_turn_attribution.py", "test_trajectory.py",
        "test_empty_reply.py", "test_answer_eval.py", "test_eval_confusion.py",
        "test_thread_followup.py", "test_multi_turn_offline.py",
    )),
    ("rag", "RAG·检索/引用溯源/评测语料", (
        "test_rag*.py", "test_grounding.py", "test_grounding_kb.py",
        "test_embedding.py", "test_qdrant_store.py", "test_red_team.py",
    )),
    ("memory", "记忆与会话/流式", (
        "test_memory*.py", "test_chat*.py",
        "test_session_lock_backend.py", "test_session_reference.py",
    )),
    ("infra", "基础设施·LLM 传输/缓存/并发/队列/MCP", (
        "test_llm*.py", "test_cache*.py", "test_p2_caches.py", "test_lock_async.py",
        "test_resilience.py", "test_retry_policy.py",
        "test_mcp*.py", "test_async_offload.py", "test_worker_features.py",
        "test_queue_and_stream_integrity.py", "test_stream_latency.py",
        "test_stack_portability.py", "test_about_toc.py",
    )),
    ("misc", "其他·杂项", ("*",)),
]

IMAGE_GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    ("plan", "规划/Prompt/商品上下文", (
        "test_image_slots_prompt.py", "test_image_prompt_limit.py",
        "test_image_llm_parse_wiring.py", "test_image_batch_context.py",
        "test_image_batch_context_zero_window.py", "test_image_product_source.py",
        "test_image_catalog_mode.py", "test_image_colors.py",
        "test_image_capabilities.py", "test_image_config.py", "test_image_schemas.py",
        "test_image_schema_dialect.py", "test_image_decoupling.py", "test_image_eval.py",
    )),
    ("generate", "生成/重试/并发/成本", (
        "test_image_provider*.py", "test_image_llm_cost_accounting.py",
        "test_image_cost_budget.py", "test_image_backfill_cost_split.py",
        "test_image_failure_usage_accounting.py", "test_image_partial_cost_accounting.py",
        "test_image_rate_limit.py", "test_image_p2_scale.py",
    )),
    ("gate", "门禁/Critic/像素/校验", (
        "test_image_gate*.py", "test_image_compliance.py", "test_image_critic.py",
        "test_image_pixels_autofix.py", "test_image_validate.py",
        "test_image_validation_order.py", "test_image_consistency_v11.py",
    )),
    ("reflexion", "Reflexion/修复库/RAG", (
        "test_image_rag*.py", "test_image_memory_repair.py",
        "test_image_repair_kb_recall.py", "test_image_retry_copy.py",
        "test_image_iterate_lineage.py",
    )),
    ("task", "任务状态机/可靠性/幂等/配额/结算", (
        "test_image_task_deadline.py", "test_image_reliability.py",
        "test_image_idempotency*.py", "test_image_quota*.py",
        "test_image_cancel*.py", "test_image_retry_cas.py",
        "test_image_settle_transaction_scope.py", "test_image_watchdog_claim.py",
        "test_image_outbox_replay2.py", "test_image_storage_outbox_fixes.py",
        "test_image_request_prune.py", "test_image_round*.py",
    )),
    ("worker", "worker 契约/存储/服务化/中台回写", (
        "test_image_worker_phase*.py", "test_image_storage.py",
        "test_image_platform_*.py",
    )),
    ("api", "API/前端/指标/资源/批量", (
        "test_image_api.py", "test_image_admin*.py", "test_image_auth.py",
        "test_image_pages.py", "test_image_frontend_v2.py",
        "test_image_metrics*.py", "test_image_stage_metrics.py",
        "test_image_payload_contract.py", "test_image_trace_link.py",
        "test_image_async_offload.py", "test_image_listing_iterate.py",
        "test_image_batch_ops.py", "test_image_batch_summary2.py",
        "test_image_partial_*.py", "test_image_ref*.py", "test_image_department_guard.py",
        "test_image_feedback*.py", "test_image_mcp_tools.py", "test_image_graph.py",
    )),
    ("misc", "其他·杂项", ("*",)),
]

SUITES: dict[str, dict] = {
    "sku": {"title": "SKU 侧（src/app/**）", "groups": SKU_GROUPS},
    "image": {"title": "生图 Agent（src/app/agents_image/**）", "groups": IMAGE_GROUPS},
}


def _test_files(suite: str) -> list[str]:
    """该 suite 覆盖的用例文件名（已排序）。sku 侧排除 test_image_*。"""
    names = sorted(path.name for path in TESTS.glob("test_*.py"))
    if suite == "sku":
        return [name for name in names if not name.startswith("test_image_")]
    return [name for name in names if name.startswith("test_image_")]


def assign(suite: str) -> dict[str, list[str]]:
    """文件名 → 分组 slug（first match wins），返回按分组顺序排列的映射。"""
    groups = SUITES[suite]["groups"]
    buckets: dict[str, list[str]] = {slug: [] for slug, _title, _patterns in groups}
    for name in _test_files(suite):
        for slug, _title, patterns in groups:
            if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
                buckets[slug].append(name)
                break
    return buckets


def group_files(suite: str, slug: str) -> list[str]:
    buckets = assign(suite)
    if slug not in buckets:
        raise SystemExit(f"[ci_split] 没有这个分组：{suite}:{slug}"
                         f"（可用：{', '.join(buckets)}）")
    return [f"tests/{name}" for name in buckets[slug]]


def verify(strict: bool) -> int:
    problems: list[str] = []
    for suite in SUITES:
        buckets = assign(suite)
        files = _test_files(suite)
        assigned = [name for names in buckets.values() for name in names]
        missing = sorted(set(files) - set(assigned))
        duplicated = sorted({name for name in assigned if assigned.count(name) > 1})
        empty = [slug for slug, names in buckets.items() if not names]
        print(f"[ci_split] {suite}（{SUITES[suite]['title']}）：{len(files)} 个用例文件")
        for slug, title, _patterns in SUITES[suite]["groups"]:
            print(f"    {slug:<10} {title:<34} {len(buckets[slug]):>3}")
        if missing:
            problems.append(f"{suite} 有 {len(missing)} 个用例没归类：{missing[:5]}")
        if duplicated:
            problems.append(f"{suite} 有用例被重复归类：{duplicated[:5]}")
        if strict and buckets.get("misc"):
            problems.append(f"{suite} 的 misc 组不为空（新用例请归类）：{buckets['misc']}")
        if empty:
            print(f"[ci_split] 提示：{suite} 有空分组：{empty}")
    if problems:
        for problem in problems:
            print(f"[ci_split] ✗ {problem}", file=sys.stderr)
        return 1
    print("[ci_split] ✓ 分组覆盖完整、无重叠")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="按项目功能拆分用例文件")
    parser.add_argument("--list", action="store_true", help="列出分组与文件数")
    parser.add_argument("--files", metavar="SUITE:GROUP", help="打印该组的用例路径")
    parser.add_argument("--verify", action="store_true", help="校验分组覆盖与重叠")
    parser.add_argument("--strict", action="store_true", help="配合 --verify：misc 必须为空")
    args = parser.parse_args(argv[1:])

    if args.files:
        if ":" not in args.files:
            print("[ci_split] --files 需要写成 SUITE:GROUP，例如 sku:rag", file=sys.stderr)
            return 2
        suite, slug = args.files.split(":", 1)
        if suite not in SUITES:
            print(f"[ci_split] 未知 suite：{suite}（可用：{', '.join(SUITES)}）", file=sys.stderr)
            return 2
        print(" ".join(group_files(suite, slug)))
        return 0
    if args.verify:
        return verify(args.strict)
    # 默认 / --list
    for suite, spec in SUITES.items():
        print(f"{suite}: {spec['title']}")
        for slug, title, _patterns in spec["groups"]:
            print(f"    {slug:<10} {title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
