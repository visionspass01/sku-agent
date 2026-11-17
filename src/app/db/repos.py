"""数据访问层：所有 SQL 收敛在这里，Agent/MCP/API 不直接写 SQL。"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Anomaly,
    CompetitorListing,
    EvalLabel,
    Flow,
    InspectionResult,
    InspectionRun,
    InTransit,
    KbChunk,
    KbDoc,
    Notification,
    PlatformActivity,
    Sku,
    StatHour,
    Supplier,
    TransferOrder,
    Warehouse,
)


# ---------------------------------------------------------------- SKU / 基础资料
def list_skus(
    session: Session,
    q: str | None = None,
    category: str | None = None,
    season: str | None = None,
    limit: int = 50,
    categories: list[str] | None = None,
) -> list[Sku]:
    stmt = select(Sku)
    if categories:
        stmt = stmt.where(Sku.category.in_(categories))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            Sku.sku_id.like(like) | Sku.style_id.like(like) | Sku.style_name.like(like)
        )
    if category:
        stmt = stmt.where(Sku.category == category)
    if season:
        stmt = stmt.where(Sku.season == season)
    return list(session.scalars(stmt.order_by(Sku.sku_id).limit(limit)))


def count_skus(session: Session, categories: list[str] | None = None) -> int:
    stmt = select(func.count()).select_from(Sku)
    if categories:
        stmt = stmt.where(Sku.category.in_(categories))
    return int(session.scalar(stmt) or 0)


def get_sku(session: Session, sku_id: str) -> Sku | None:
    return session.get(Sku, sku_id)


def get_style_skus(session: Session, style_id: str) -> list[Sku]:
    return list(
        session.scalars(
            select(Sku).where(Sku.style_id == style_id).order_by(Sku.size)
        )
    )


def get_supplier(session: Session, supplier_id: str) -> Supplier | None:
    return session.get(Supplier, supplier_id)


def list_warehouses(session: Session) -> list[Warehouse]:
    return list(session.scalars(select(Warehouse)))


# ---------------------------------------------------------------- 销量 / 快照
def get_stat_hours(
    session: Session, sku_id: str, start: dt.datetime, end: dt.datetime
) -> list[tuple[dt.datetime, int]]:
    rows = session.execute(
        select(StatHour.stat_hour, StatHour.qty)
        .where(StatHour.sku_id == sku_id, StatHour.stat_hour >= start, StatHour.stat_hour <= end)
        .order_by(StatHour.stat_hour)
    )
    return [(r[0], int(r[1])) for r in rows]


def get_stat_range(
    session: Session, sku_ids: list[str], start: dt.datetime, end: dt.datetime
) -> dict[str, list[tuple[dt.datetime, int]]]:
    rows = session.execute(
        select(StatHour.sku_id, StatHour.stat_hour, StatHour.qty).where(
            StatHour.sku_id.in_(sku_ids),
            StatHour.stat_hour >= start,
            StatHour.stat_hour <= end,
        )
    )
    out: dict[str, list[tuple[dt.datetime, int]]] = {s: [] for s in sku_ids}
    for sku_id, hour, qty in rows:
        out[sku_id].append((hour, int(qty)))
    for v in out.values():
        v.sort()
    return out


def latest_stat_hour(session: Session) -> dt.datetime | None:
    return session.scalar(select(func.max(StatHour.stat_hour)))


def get_flows(session: Session, start: dt.datetime, end: dt.datetime) -> list[Flow]:
    return list(
        session.scalars(
            select(Flow).where(Flow.ts >= start, Flow.ts <= end).order_by(Flow.ts)
        )
    )


# ---------------------------------------------------------------- 在途 / 调拨 / 竞品
def get_in_transit(session: Session, sku_id: str) -> list[InTransit]:
    return list(
        session.scalars(select(InTransit).where(InTransit.sku_id == sku_id).order_by(InTransit.eta))
    )


def get_transfers(session: Session, sku_id: str) -> list[TransferOrder]:
    return list(
        session.scalars(
            select(TransferOrder).where(TransferOrder.sku_id == sku_id).order_by(TransferOrder.eta)
        )
    )


def get_competitors(session: Session, style_id: str, platform: str | None = None) -> list[CompetitorListing]:
    stmt = select(CompetitorListing).where(CompetitorListing.style_id == style_id)
    if platform:
        stmt = stmt.where(CompetitorListing.platform == platform)
    return list(session.scalars(stmt.order_by(CompetitorListing.price)))


def upcoming_activities(
    session: Session, platform: str, from_date: dt.date, days: int = 45
) -> list[PlatformActivity]:
    end = from_date + dt.timedelta(days=days)
    return list(
        session.scalars(
            select(PlatformActivity).where(
                PlatformActivity.platform.in_([platform, "全部"]),
                PlatformActivity.end_date >= from_date,
                PlatformActivity.start_date <= end,
            )
        )
    )


# ---------------------------------------------------------------- 异常 / 评估
def replace_anomalies(session: Session, hour: dt.datetime, anomalies: list[dict]) -> None:
    session.execute(delete(Anomaly).where(Anomaly.hour == hour))
    for a in anomalies:
        session.add(
            Anomaly(
                sku_id=a["sku_id"],
                hour=hour,
                severity=a.get("severity", "中"),
                reasons=a.get("reasons", []),
                created_at=dt.datetime.now(),
            )
        )


def list_anomalies(session: Session, limit: int = 200, categories: list[str] | None = None) -> list[Anomaly]:
    stmt = select(Anomaly).join(Sku, Anomaly.sku_id == Sku.sku_id)
    if categories:
        stmt = stmt.where(Sku.category.in_(categories))
    return list(
        session.scalars(stmt.order_by(Anomaly.severity.desc(), Anomaly.id.desc()).limit(limit))
    )


def count_anomalies(session: Session, categories: list[str] | None = None) -> int:
    stmt = select(func.count()).select_from(Anomaly).join(Sku, Anomaly.sku_id == Sku.sku_id)
    if categories:
        stmt = stmt.where(Sku.category.in_(categories))
    return int(session.scalar(stmt) or 0)


def breakdown_by_department(session: Session) -> dict:
    """按部门（男装/女装）统计 SKU 数与异常数，供管理员点击卡片查看明细。"""
    from app.config import ALL_DEPARTMENTS, categories_for

    result: dict[str, dict] = {}
    for dept in ALL_DEPARTMENTS:
        cats = categories_for(dept)
        result[dept] = {
            "sku_count": count_skus(session, cats),
            "anomaly_count": count_anomalies(session, cats),
        }
    return result


def get_anomaly(session: Session, sku_id: str) -> Anomaly | None:
    return session.scalars(
        select(Anomaly).where(Anomaly.sku_id == sku_id).order_by(Anomaly.id.desc())
    ).first()


def get_eval_label(session: Session, sku_id: str) -> EvalLabel | None:
    return session.get(EvalLabel, sku_id)


def all_eval_labels(session: Session) -> list[EvalLabel]:
    return list(session.scalars(select(EvalLabel)))


# ---------------------------------------------------------------- 巡检运行
def create_run(session: Session, kind: str, params: dict | None = None) -> InspectionRun:
    run = InspectionRun(
        id=str(uuid.uuid4()),
        kind=kind,
        status="running",
        params=params or {},
        stats={},
        started_at=dt.datetime.now(),
    )
    session.add(run)
    return run


def get_run(session: Session, run_id: str) -> InspectionRun | None:
    return session.get(InspectionRun, run_id)


def update_run_params(session: Session, run_id: str, **extra) -> None:
    """增量写 inspection_run.params（如 thread_id），不覆盖已有键。"""
    run = session.get(InspectionRun, run_id)
    if run is None:
        return
    params = dict(run.params or {})
    params.update(extra)
    run.params = params


def list_runs(session: Session, limit: int = 20) -> list[InspectionRun]:
    return list(session.scalars(select(InspectionRun).order_by(InspectionRun.started_at.desc()).limit(limit)))


def finish_run(session: Session, run_id: str, status: str, stats: dict, error: str | None = None) -> None:
    run = session.get(InspectionRun, run_id)
    if run is None:
        return
    run.status = status
    run.stats = stats
    run.finished_at = dt.datetime.now()
    run.error = error


def add_result(
    session: Session,
    run_id: str,
    sku_id: str,
    risk_level: str,
    plan: dict,
    citations: list,
    verifier: dict,
    data_bundle: dict,
    degraded: bool,
    elapsed_ms: int,
) -> InspectionResult:
    row = InspectionResult(
        run_id=run_id,
        sku_id=sku_id,
        risk_level=risk_level,
        plan=plan,
        citations=citations,
        verifier=verifier,
        data_bundle=data_bundle,
        degraded=degraded,
        elapsed_ms=elapsed_ms,
        created_at=dt.datetime.now(),
    )
    session.add(row)
    return row


def get_results(session: Session, run_id: str, categories: list[str] | None = None) -> list[InspectionResult]:
    stmt = select(InspectionResult).where(InspectionResult.run_id == run_id)
    if categories:
        stmt = stmt.join(Sku, InspectionResult.sku_id == Sku.sku_id).where(Sku.category.in_(categories))
    return list(session.scalars(stmt))


def get_result_by_sku(session: Session, sku_id: str) -> InspectionResult | None:
    return session.scalars(
        select(InspectionResult)
        .where(InspectionResult.sku_id == sku_id)
        .order_by(InspectionResult.id.desc())
    ).first()


# ---------------------------------------------------------------- 知识库
def get_kb_doc(session: Session, doc_id: str) -> KbDoc | None:
    return session.get(KbDoc, doc_id)


def list_kb_docs(session: Session) -> list[KbDoc]:
    return list(session.scalars(select(KbDoc).order_by(KbDoc.doc_id)))


def list_kb_chunks(session: Session) -> list[KbChunk]:
    return list(session.scalars(select(KbChunk).order_by(KbChunk.doc_id, KbChunk.ord)))


def get_kb_chunk(session: Session, chunk_id: str) -> KbChunk | None:
    return session.get(KbChunk, chunk_id)


def add_notification(
    session: Session, run_id: str, title: str, body: str, channel: str = "企微"
) -> Notification:
    n = Notification(run_id=run_id, channel=channel, title=title, body=body, created_at=dt.datetime.now())
    session.add(n)
    return n
