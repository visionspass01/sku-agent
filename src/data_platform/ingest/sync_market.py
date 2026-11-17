"""市场域接入：竞品同款行情 + 平台大促窗口。

数据来自「拍立淘」这类图片同款检索与平台营销日历。放在中台的原因：
竞品价格带是**公司级事实**，不是某个 Agent 的私有数据——生图 Agent 以后也要用它定价。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from data_platform.db import RawCompetitorListing, RawPlatformActivity
from data_platform.ingest.common import clear_table
from data_platform.mock_sources import load_snapshot


def sync_market(session: Session, run_id: str = "manual") -> int:
    clear_table(session, RawCompetitorListing)
    competitors = load_snapshot("competitors.json")["items"]
    for it in competitors:
        session.add(RawCompetitorListing(
            style_id=it["style_id"], shop_name=it.get("shop_name", ""),
            platform=it.get("platform", "淘系"), price=float(it.get("price") or 0),
            sales_30d=int(it.get("sales_30d") or 0), sales_7d=int(it.get("sales_7d") or 0),
            rating=float(it.get("rating") or 0), trend_pct=float(it.get("trend_pct") or 0),
            snapshot_ts=dt.datetime.fromisoformat(str(it.get("snapshot_ts")
                                                      or dt.datetime.now().isoformat(timespec="seconds"))),
        ))

    clear_table(session, RawPlatformActivity)
    activities = load_snapshot("platform_activities.json")["items"]
    for it in activities:
        session.add(RawPlatformActivity(
            name=it["name"], platform=it.get("platform", "全部"),
            start_date=dt.date.fromisoformat(str(it["start_date"])[:10]),
            end_date=dt.date.fromisoformat(str(it["end_date"])[:10]),
            category=it.get("category", "全部"), note=it.get("note", ""),
        ))
    session.flush()
    return len(competitors) + len(activities)
