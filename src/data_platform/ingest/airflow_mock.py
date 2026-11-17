"""接入层：模拟 Airflow 调度 DAG。

真实项目里由 Airflow 编排 dag（sync → quality → dbt）；Demo 用轻量 Python 模拟：
任务依赖、失败重试 1 次、运行日志写入 etl_run_log，便于审计与排障演示。
"""

from __future__ import annotations

import datetime as dt
import time
import uuid

from data_platform.db import EtlRunLog, session_scope


def _log_start(session, dag_id, task_id, run_id) -> EtlRunLog:
    row = EtlRunLog(dag_id=dag_id, task_id=task_id, run_id=run_id, status="running",
                    started_at=dt.datetime.now())
    session.add(row)
    session.flush()
    return row


def _log_end(session, row, status, rows, error=None) -> None:
    row.status = status
    row.rows = int(rows or 0)
    row.finished_at = dt.datetime.now()
    row.error = error


def _rows_of(result) -> int:
    if isinstance(result, int):
        return result
    if isinstance(result, dict):
        rows = result.get("rows", 0)
        if isinstance(rows, dict):
            return int(sum(rows.values()))
        return int(rows or 0)
    return 0


def run_dag(dag_id: str = "daily_sync", run_id: str | None = None) -> dict:
    """执行 DAG：各源接入 → quality_check → dbt_run。

    任务顺序按「依赖 + 爆炸半径」排：ERP 目录 → CRM 客户 → 订单 → 履约（物流/售后）→
    市场（竞品/活动）→ 供应链（供应商/调拨）→ 质量校验 → 转换。
    每个源单独一个任务：物流接口挂了不该把订单表也清空（分开重试、分开留痕）。
    CRM 客户排在订单之前：订单的 customer_id 取自 CRM 客户集合，先到客户再落订单，
    便于按客户维度排查；两者之间没有数据库外键，因此**不声明强依赖**（CRM 失败不应连带订单失败）。
    """
    from data_platform.dbt_like.run_transforms import run_transforms
    from data_platform.ingest.sync_crm import sync_crm
    from data_platform.ingest.sync_erp import sync_erp
    from data_platform.ingest.sync_logistics import sync_after_sales, sync_logistics
    from data_platform.ingest.sync_market import sync_market
    from data_platform.ingest.sync_orders import sync_orders
    from data_platform.ingest.sync_supply import sync_supply

    run_id = run_id or str(uuid.uuid4())
    sync_tasks = ("sync_erp", "sync_crm", "sync_orders", "sync_logistics", "sync_after_sales",
                  "sync_market", "sync_supply")
    tasks = {
        "sync_erp": (sync_erp, ()),
        "sync_crm": (sync_crm, ()),
        "sync_orders": (sync_orders, ()),
        "sync_logistics": (sync_logistics, ("sync_orders",)),
        "sync_after_sales": (sync_after_sales, ("sync_orders",)),
        "sync_market": (sync_market, ()),
        "sync_supply": (sync_supply, ("sync_erp",)),
        "quality_check": (_quality_check, sync_tasks),
        "dbt_run": (lambda s: run_transforms(s), ("quality_check",)),
    }
    order = [*sync_tasks, "quality_check", "dbt_run"]
    done: dict[str, dict] = {}
    for task_id in order:
        fn, deps = tasks[task_id]
        for dep in deps:
            if done.get(dep, {}).get("status") != "success":
                raise RuntimeError(f"任务 {task_id} 依赖 {dep} 未成功，DAG 中止")
        rows = 0
        error = None
        status = "success"
        started = time.monotonic()
        for attempt in range(2):  # 失败重试 1 次
            try:
                with session_scope() as session:
                    log = _log_start(session, dag_id, task_id, run_id)
                    result = fn(session)
                    rows = _rows_of(result)
                    _log_end(session, log, "success", rows)
                error = None
                break
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                with session_scope() as session:
                    log = EtlRunLog(dag_id=dag_id, task_id=task_id, run_id=run_id,
                                    status="failed", rows=0, started_at=dt.datetime.now(),
                                    finished_at=dt.datetime.now(), error=error[:1000])
                    session.add(log)
                if attempt == 0:
                    continue
                status = "failed"
        done[task_id] = {"status": status, "rows": rows, "error": error,
                         "elapsed_ms": int((time.monotonic() - started) * 1000)}
        if status == "failed":
            break
    return {"dag_id": dag_id, "run_id": run_id, "tasks": done}


def _quality_check(session) -> int:
    """基础数据质量校验：关键源必须非空、字段合法、跨源能对上。

    校验项刻意选「错了会让下游结论失真」的那种：
    - 库存为负 / 订单为空 → mart 全错；
    - **售后数量 > 订单数量** → 退货率会超过 100%，趋势判断失真；
    - **已发货订单缺物流单** → 「这单到哪了」永远答不出来（口径缺口而非峰值问题）。
    """
    from sqlalchemy import func, select

    from data_platform.db import (
        RawAfterSalesTicket,
        RawCrmCustomer,
        RawErpInventory,
        RawLogistics,
        RawOrder,
        RawSupplier,
    )

    inv = session.scalar(select(func.count()).select_from(RawErpInventory)) or 0
    orders = session.scalar(select(func.count()).select_from(RawOrder)) or 0
    customers = session.scalar(select(func.count()).select_from(RawCrmCustomer)) or 0
    if inv == 0 or orders == 0 or customers == 0:
        raise ValueError(f"数据质量校验失败：inventory={inv}, orders={orders}, customers={customers}")
    bad = session.scalar(
        select(func.count()).select_from(RawErpInventory).where(RawErpInventory.on_hand < 0)
    )
    if bad:
        raise ValueError(f"数据质量校验失败：{bad} 条库存为负数")

    logistics = session.scalar(select(func.count()).select_from(RawLogistics)) or 0
    if logistics == 0:
        raise ValueError("数据质量校验失败：物流单为空（订单域履约事实缺失）")
    shipped_without_logistics = session.scalar(
        select(func.count()).select_from(RawOrder)
        .outerjoin(RawLogistics, RawLogistics.order_id == RawOrder.order_id)
        .where(RawOrder.status == "已发货", RawLogistics.order_id.is_(None))
    )
    if shipped_without_logistics:
        raise ValueError(f"数据质量校验失败：{shipped_without_logistics} 条已发货订单缺物流单")

    ticket_qty = dict(session.execute(
        select(RawAfterSalesTicket.order_id, func.sum(RawAfterSalesTicket.qty))
        .group_by(RawAfterSalesTicket.order_id)
    ).all())
    if ticket_qty:
        order_qty = dict(session.execute(
            select(RawOrder.order_id, RawOrder.qty).where(RawOrder.order_id.in_(list(ticket_qty)))
        ).all())
        over = [oid for oid, qty in ticket_qty.items()
                if order_qty.get(oid) is not None and int(qty or 0) > int(order_qty[oid] or 0)]
        if over:
            raise ValueError(f"数据质量校验失败：{len(over)} 个订单的售后数量超过购买数量（示例 {over[:3]}）")

    suppliers = session.scalar(select(func.count()).select_from(RawSupplier)) or 0
    if suppliers == 0:
        raise ValueError("数据质量校验失败：供应商主数据为空（补货交期无从计算）")
    return inv + orders + customers + logistics + suppliers
