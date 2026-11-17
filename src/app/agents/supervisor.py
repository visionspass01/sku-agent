"""Supervisor 节点：LLM 任务拆解 + 并行 Worker 分发（Supervisor-Worker 架构）。"""

from __future__ import annotations

import time

from app.agents import handoff
from app.agents.prompt_loader import load_prompt
from app.agents.schemas import SupervisorPlan
from app.db import repos
from app.db.engine import session_scope
from app.llm import get_llm
from app.mcp_client.registry import tools_prompt

# 提示词按调用加载（有 mtime 缓存）：改 md 即生效，不需要重启进程
SUPERVISOR_PROMPT = "supervisor"


async def supervisor_node(state: dict) -> dict:
    started = time.monotonic()
    sku_id = state["sku_id"]
    with session_scope() as session:
        sku = repos.get_sku(session, sku_id)
        if sku is None:
            raise ValueError(f"SKU 不存在: {sku_id}")
        brief = (
            f"sku_id={sku.sku_id} 款号={sku.style_id} 品类={sku.category} 季节={sku.season}\n"
            f"库存={sku.stock} 退货率={sku.return_rate} 平台={sku.platform} 仓={sku.warehouse_id}\n"
            f"售价={sku.price} 供应商={sku.supplier_id}"
        )
        category = sku.category
        season = sku.season

    user = (
        f"{tools_prompt()}\n\n"
        f"现在处理该 SKU（as_of={state.get('as_of')}）：\n{brief}\n"
        "请输出任务拆解 JSON。"
    )
    note = "默认全量派发（erp/market/rag）"
    degraded = False
    degrade_reason = ""
    try:
        plan = await get_llm().invoke_json(
            load_prompt(SUPERVISOR_PROMPT), user, SupervisorPlan, max_tokens=300
        )
        tasks = [t for t in plan.tasks if t]
        if "erp" not in tasks:
            tasks.append("erp")
    except Exception as exc:  # noqa: BLE001
        # LLMError（JSON 修复失败）与网络/限流错误都要降级成「默认全量派发」；
        # 早先只捕 LLMError，LLM 网络故障会把整个巡检 run 直接抛崩。
        degraded = True
        degrade_reason = type(exc).__name__
        tasks = ["erp", "market", "rag"]
        plan = SupervisorPlan(tasks=["erp", "market", "rag"], priority="中",
                              note=f"{note}（LLM 不可用降级：{degrade_reason}）")

    # 交接单：Supervisor 把「采集子集」交给 collector（字段固定 + 写进 trace，便于复盘）
    contract = handoff.start(
        "supervisor", "collector",
        reason=f"按 plan.priority={plan.priority} 派发采集子集",
        payload={"tasks": tasks, "sku_id": sku_id, "as_of": state.get("as_of") or ""},
        expected="各子集原始数据（erp/market/rag），缺失的 source 不发起调用",
        trace_id=str(state.get("run_id") or ""),
    )
    return {
        "supervisor": plan.model_dump(),
        "rag_query": (
            f"{category} {season} 库存管理 补货 断码 清仓 换季 平台活动 供应商交期"
        ),
        "trace": [{
            "node": "supervisor",
            "status": "degraded" if degraded else "ok",
            "ms": int((time.monotonic() - started) * 1000),
            "detail": (f"tasks={tasks} priority={plan.priority}"
                       + (f" degraded=True reason={degrade_reason}" if degraded else "")),
        }, handoff.finish(contract, started)],
    }
