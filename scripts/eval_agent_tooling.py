"""Agent 工具面与写路径门禁（复审 A1/A2/A3/A4/A7/R4/F1/F2/S1）：确定性、零 token、可作 CI 门槛。

为什么单独一个脚本：这几条都是"模型看到什么 / 写操作怎么保证幂等 / 失败怎么表达 / 能看到谁的档案"，
它们不是业务精度，而是**契约**。单测能覆盖行为，但没人会每天翻单测报告；
脚本给的是一个能贴进 CI 的矩阵——谁把工具描述删了、谁把幂等键交还给模型了、
谁把跨部门档案放出去了，PR 上直接红。

八组检查：
1. 工具面（A1）：描述非空且够长、**每个参数都有说明**、写工具写明使用条件；
2. 幂等（A2）：同参同键、异参异键、模型自造的幂等 token 影响不到键；
3. 未知工具/非法参数（A3/R2）：hook 抛出可读异常，路由层翻译成 422 + `X-Error-Code` + fatal；
4. 证据下限（A4）：预案 evidence < 3 条在 schema 层失败；
5. 聚合口径（A7）：没检查的维度 rate 是 `None` 而不是 100%；
6. 知识面口径（R4）：skills/*.md 的断码/补货/清仓口径与代码实现一致；
7. 档案可见性（F1/F2）：档案按内容归部门、跨部门读不到、「全员」仍可读、部门账号不能写；
8. 引用解析（S1）：`@session` 要么命中、要么 `denied`/`not_found`，不拿最近档案顶上。

用法：
    python scripts/eval_agent_tooling.py --min-pass 1.0
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

WRITE_POLICY_HINTS = ("仅", "人工确认", "不要", "不会自动")
MIN_DESCRIPTION_CHARS = 12


def _setup(tmp: pathlib.Path):  # noqa: ANN201 —— 返回 Settings
    """独立临时库：工具契约会写 `tool_result` 台账，不能落到演示库上。"""
    from app.config import Settings, set_settings
    from app.db.engine import init_schema, make_engine, set_engine

    settings = Settings(
        database_url=f"sqlite:///{(tmp / 'tooling.db').as_posix()}",
        redis_url="",
        mcp_transport="direct",
        scheduler_enabled=False,
        grounding_enabled=False,
        catalog_sync_on_startup=False,
        platform_api_url="http://127.0.0.1:8001",
        platform_timeout_seconds=2.0,
        deepseek_api_key="",
    )
    # 档案主文档落在 analyses_dir：必须指向临时目录，否则会往仓库 data/analyses/ 里写文件
    settings.analyses_dir = str(tmp / "analyses")
    set_settings(settings)
    engine = make_engine(settings)
    set_engine(engine)
    init_schema(engine)

    # 种子：两个不同部门的 SKU（档案部门推断靠 catalog → 本地 Sku 表兜底）
    from app.db.engine import session_scope
    from app.db.models import Sku

    with session_scope() as session:
        for sku_id, style_id, category, size in (
            ("ST-90-C1-M", "ST-90", "女装", "M"),
            ("ST-92-C1-M", "ST-92", "男装", "M"),
        ):
            session.add(Sku(
                sku_id=sku_id, style_id=style_id, style_name=f"{style_id} 款",
                category=category, season="秋冬", color="黑色", size=size,
                warehouse_id="WH-HZ", platform="淘系", price=299, list_price=299,
                stock=60, return_rate=0.05, supplier_id="SUP-01", status="在售",
            ))
    return settings


def _cases() -> list[dict]:
    import httpx

    from app.agents.answer_eval import aggregate
    from app.agents.schemas import InspectionPlan, PlanAction
    from app.agents.tool_guard import InvalidToolCallError, validate_tool_calls
    from app.auth import AuthUser
    from app.memory.runtime import tool_context, turn_log_context
    from app.tools import platform_tools
    from app.tools.platform_tools import (
        WRITE_TOOL_NAMES,
        build_platform_tools,
        expected_tool_count,
    )

    admin = AuthUser(username="admin", role="admin", department=None)
    results: list[dict] = []

    def case(name: str, ok: bool, detail: object = "") -> None:
        results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})

    # ---------------------------------------------------------------- ① 工具面（A1）
    tools = build_platform_tools(admin)
    case("工具装配数与 expected_tool_count 同源",
         len(tools) == expected_tool_count(["*"]),
         f"{len(tools)} vs {expected_tool_count(['*'])}")

    thin = [(t.name, len(t.description or "")) for t in tools
            if len((t.description or "").strip()) < MIN_DESCRIPTION_CHARS]
    case(f"每个工具都有 ≥{MIN_DESCRIPTION_CHARS} 字的描述（模型可见）", not thin, thin)

    missing_params: list[str] = []
    schema_drift: list[str] = []
    for tool in tools:
        props = (tool.args_schema.model_json_schema().get("properties") or {}) \
            if tool.args_schema else {}
        for name, spec in props.items():
            if not str(spec.get("description") or "").strip():
                missing_params.append(f"{tool.name}.{name}")
        signature = inspect.signature(tool.coroutine)
        expected = {n for n, p in signature.parameters.items()
                    if n not in ("self", "cls")
                    and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)}
        if set(props) != expected:
            schema_drift.append(f"{tool.name}: {sorted(set(props) ^ expected)}")
    case("每个工具参数都有说明（args_schema description）", not missing_params, missing_params)
    case("args_schema 与函数签名一致（无漂移）", not schema_drift, schema_drift)

    by_name = {t.name: t for t in tools}
    no_policy = [name for name in sorted(WRITE_TOOL_NAMES)
                 if not any(h in (by_name[name].description or "") for h in WRITE_POLICY_HINTS)]
    case("写工具描述写明使用条件（仅在/人工确认/不要…）", not no_policy, no_policy)

    save_desc = by_name["save_analysis"].description or ""
    case("save_analysis 明确「仅在用户要求时调用」",
         "仅" in save_desc and any(w in save_desc for w in ("沉淀", "留档", "存档")),
         save_desc[:80])

    # ---------------------------------------------------------------- ② 幂等（A2）
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/erp/replenish-orders"):
            body = json.loads(request.content.decode("utf-8"))
            posted.append(body)
            return httpx.Response(200, json={"status": "pending_confirmation",
                                             "po_no": f"PO-{len(posted)}"})
        return httpx.Response(404, json={"detail": "not found"})

    platform_tools.set_platform_client(platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler)))
    order_tool = {t.name: t for t in build_platform_tools(
        admin, allow=["create_replenishment_order"])}["create_replenishment_order"]

    signature = inspect.signature(order_tool.coroutine).parameters
    case("补货单签名不含模型可控的幂等键（request_id/idempotency_key）",
         "request_id" not in signature and "idempotency_key" not in signature,
         sorted(signature))

    async def _call(**args):  # noqa: ANN003
        with tool_context(session_id="s-eval", username="admin", department=None,
                          agent_id="operator"):
            with turn_log_context("t-eval", "s-eval"):
                return await order_tool.ainvoke(args)

    asyncio.run(_call(sku_id="ST-01-C1-M", qty=120, reason="断码补货"))
    asyncio.run(_call(sku_id="ST-01-C1-M", qty=120, reason="断码补货"))
    asyncio.run(_call(sku_id="ST-01-C1-M", qty=120, reason="断码补货",
                      request_id="model-invented"))
    keys = [body.get("idempotency_key") for body in posted]
    case("同参重放 + 模型硬塞 request_id 仍然同键（幂等不可被模型影响）",
         len(posted) == 3 and len(set(keys)) == 1, keys)
    asyncio.run(_call(sku_id="ST-01-C1-M", qty=150, reason="断码补货"))
    case("参数变化会产生新键（不会把不同请求合并）", posted[-1]["idempotency_key"] != keys[0],
         posted[-1].get("idempotency_key"))
    platform_tools.set_platform_client(None)

    # ---------------------------------------------------------------- ③ 未知工具名（A3）
    hook = validate_tool_calls({"get_inventory"})
    from langchain_core.messages import AIMessage

    bad_call = AIMessage(content="", tool_calls=[{"name": "get_inventory_v2",
                                                  "args": {"sku_id": "S1"}, "id": "c1",
                                                  "type": "tool_call"}])
    try:
        hook({"messages": [bad_call]})
        raised: BaseException | None = None
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    case("未知工具名被提前拦下（不是空消息 RuntimeError）",
         isinstance(raised, InvalidToolCallError) and "get_inventory_v2" in str(raised),
         type(raised).__name__)
    case("异常携带 bad_tools / allowed_tools",
         bool(getattr(raised, "bad_tools", None))
         and "get_inventory" in getattr(raised, "allowed_tools", []),
         getattr(raised, "bad_tools", None))

    from app.api.chat_routes import AGENT_FAILURE_TEMPLATES, _agent_failure

    failure = _agent_failure(raised) or {}
    case("路由层翻译成 422 + 错误码 + fatal（前端不自动重发）",
         failure.get("status") == 422
         and failure.get("error_code") == "AGENT_INVALID_TOOL_NAME"
         and failure.get("fatal") is True
         and "AGENT_INVALID_TOOL_NAME" in AGENT_FAILURE_TEMPLATES,
         failure.get("error_code"))

    # ---------------------------------------------------------------- ④ 证据下限（A4）
    def _plan(evidence: list[str]) -> dict:
        return {"risk_level": "中", "verdict": "库存偏高",
                "evidence": evidence,
                "plan": [PlanAction(action="促销", qty=None, reason="季末疲软")],
                "citations": ["sop-x#abcd1234"]}

    two_ok = False
    try:
        InspectionPlan(**_plan(["可用库存 10 件", "日均 2 件"]))
    except Exception:  # noqa: BLE001 —— 期望 ValidationError
        two_ok = True
    three_ok = True
    try:
        InspectionPlan(**_plan(["可用库存 10 件", "日均 2 件", "在途 0 件"]))
    except Exception:  # noqa: BLE001
        three_ok = False
    case("evidence < 3 条在校验层失败、= 3 条通过", two_ok and three_ok,
         {"two_rejected": two_ok, "three_ok": three_ok})

    # ---------------------------------------------------------------- ⑤ 聚合口径（A7）
    agg = aggregate([{"passed": True, "checks": {"tools": True}, "issues": []}])
    case("未检查维度 rate 为 None（不是 100%）",
         agg["dims"]["no_write"]["checked"] == 0 and agg["dims"]["no_write"]["rate"] is None,
         agg["dims"]["no_write"])

    # ---------------------------------------------------------------- ⑥ 知识面口径（R4）
    # skills/*.md 是**模型可见的提示词**：口径与实现不一致时，模型会给出与治理口径相反的
    # 结论，而数字是真的、grounding 拦不住。这里把关键口径钉成门槛。
    from app.agents.prompt_loader import PROMPT_DIR

    skills = PROMPT_DIR.parent / "skills"

    def _skill(name: str) -> str:
        return (skills / f"{name}.md").read_text(encoding="utf-8")

    gap = _skill("size-gap-diagnosis")
    case("断码 skill 用主销码口径（≥15% 且可用为 0，长尾码不算断码）",
         all(word in gap for word in ("主销码", "15%", "不算断码")),
         "缺主销码/15%/不算断码")

    screening = _skill("sku-screening")
    case("初筛口径被标注为「仅候选初筛」并指向主销码结论",
         "初筛" in screening and "主销码" in screening
         and "size-gap-diagnosis" in screening,
         "初筛口径没有限定词")

    restock = _skill("restock-calc")
    case("补货 skill 公式与 replenishment.py 一致（0.3 / 交期+3 / 交期+21 / MOQ / 500 上限）",
         all(piece in restock for piece in ("0.3", "交期 + 3", "交期 + 21", "MOQ", "500")),
         "公式与代码不一致")

    overstock = _skill("overstock-review")
    case("清仓 skill 写明折扣下限与分批放量（6 折 / 40%）",
         "6 折" in overstock and "40%" in overstock, "缺折扣或分批")

    # ---------------------------------------------------------------- ⑦ 档案可见性（F1/F2）
    # 组织记忆（L3）按部门隔离，「全员」对全部门可见。早先 admin 沉淀一律落「全员」→
    # 单部门分析被全部门读到。现在写侧按内容推断部门，这里把"写对部门 + 读不到别部门"钉成门槛。
    from app.db.engine import session_scope
    from app.memory import org_memory
    from app.memory.runtime import append_turn_step

    platform_tools.set_platform_client(None)   # 让 catalog 走本地 Sku 兜底
    worker_men = AuthUser(username="worker1", role="user", department="男装")
    tools_admin = {t.name: t for t in build_platform_tools(admin)}
    tools_men = {t.name: t for t in build_platform_tools(worker_men)}

    def _save(tools_map, user, *, question, conclusion, sku_in_steps: str = "") -> dict:
        async def _run():  # noqa: ANN202
            with tool_context(session_id="s-org", username=user.username,
                              department=user.department, agent_id="analyst"):
                with turn_log_context("t-org", "s-org"):
                    if sku_in_steps:
                        append_turn_step({"tool": "get_inventory", "status": "ok",
                                          "args": f'{{"sku_id": "{sku_in_steps}"}}'})
                    return await tools_map["save_analysis"].ainvoke({
                        "question": question, "conclusion": conclusion,
                        "key_metrics": {"available": 20}, "evidence": ["res_0011223344"],
                    })
        return asyncio.run(_run())

    women_archive = _save(tools_admin, admin, question="ST-90-C1-M 清仓怎么处理",
                          conclusion="女装 ST-90 建议 8 折分批清仓",
                          sku_in_steps="ST-90-C1-M")
    case("admin 的单部门档案按内容归到「女装」（不再是全员）",
         women_archive.get("department") == "女装"
         and women_archive.get("department_source") == "inferred_sku",
         {k: women_archive.get(k) for k in ("department", "department_source")})

    with session_scope() as session:
        hidden = org_memory.get_analysis(session, women_archive.get("analysis_id", ""),
                                        departments=["男装"])
        visible = org_memory.get_analysis(session, women_archive.get("analysis_id", ""),
                                          departments=["女装", "童装"])
    case("跨部门档案读不到（男装账号 get_analysis = None，女装账号可读）",
         hidden is None and visible is not None, {"hidden": hidden, "visible": bool(visible)})

    company_archive = _save(tools_admin, admin, question="整体动销率怎么样",
                            conclusion="全公司动销率 62%，同比持平")
    with session_scope() as session:
        read_by_men = org_memory.get_analysis(session, company_archive.get("analysis_id", ""),
                                              departments=["男装"])
    case("公司级（无部门证据）档案仍是「全员」，部门账号可读",
         company_archive.get("department") == "全员" and read_by_men is not None,
         {k: company_archive.get(k) for k in ("department", "department_source")})

    denied = _save(tools_men, worker_men, question="ST-92-C1-M 积压", conclusion="建议调拨")
    case("部门账号不能写档案（写权限拒绝桩）",
         denied.get("error_kind") == "business" and "无权限" in str(denied.get("error")),
         denied.get("error"))

    # ---------------------------------------------------------------- ⑧ 引用解析（S1）
    # `@session <id>` 必须"要么命中、要么明确失败"：早先不存在/无权限/标题没命中都会回退成
    # "最近一份档案"，模型会拿无关档案当用户引用的那份。
    from app.memory import org_memory as org_mod

    def _ref(message: str, departments: list[str] | None):  # noqa: ANN202
        with session_scope() as session:
            return org_mod.resolve_session_reference(session, message, departments=departments)

    denied_ref = _ref(f"@session {women_archive.get('analysis_id', '')} 这个还用吗", ["男装"])
    case("引用别部门档案 → status=denied 且不返回任何档案",
         denied_ref.get("status") == "denied" and denied_ref.get("items") == [],
         denied_ref.get("status"))

    missing_ref = _ref("@session 00000000-0000-0000-0000-000000000000 看看这份", ["男装"])
    case("引用不存在的 id → status=not_found（不再回退成最近档案）",
         missing_ref.get("status") == "not_found" and missing_ref.get("items") == [],
         missing_ref.get("status"))

    ok_ref = _ref(f"@session {company_archive.get('analysis_id', '')} 还用吗", ["男装"])
    case("引用可见档案 → status=ok 且带回那一条",
         ok_ref.get("status") == "ok"
         and [i.get("analysis_id") for i in ok_ref.get("items") or []]
         == [company_archive.get("analysis_id")],
         ok_ref.get("status"))

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent 工具面 / 写路径 / 错误契约门禁")
    parser.add_argument("--min-pass", type=float, default=1.0,
                        help="通过率门槛（默认 1.0：这些是契约，不许有例外）")
    parser.add_argument("--json", default="", help="把明细写到该 JSON 文件")
    args = parser.parse_args(argv)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="eval_agent_tooling_"))
    _setup(tmp)
    cases = _cases()

    passed = sum(1 for item in cases if item["ok"])
    total = len(cases)
    ratio = passed / total if total else 0.0
    for item in cases:
        print(f"{'PASS' if item['ok'] else 'FAIL'}  {item['name']}"
              + (f"  -> {item['detail']}" if not item["ok"] else ""))
    print(f"\nAgent 工具面/写路径：{passed}/{total} 通过（{ratio:.1%}）")
    if args.json:
        pathlib.Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.json).write_text(
            json.dumps({"passed": passed, "total": total, "ratio": ratio, "cases": cases},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ratio >= args.min_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
