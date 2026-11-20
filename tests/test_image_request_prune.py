"""第九轮 P2-4：幂等表 image_request 必须按保留期清理。

这张表为「同 key 重放首次响应」保存完整响应体（含整批任务列表），只增不删会无限增长；
它过去不在 runtime 清理循环的清单里。
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.agents_image.store import requests as request_store
from app.agents_image.store.models import ImageRequest
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


def _mk(request_id: str, *, status: str, age_hours: float) -> str:
    with session_scope() as session:
        session.add(ImageRequest(
            request_id=request_id, status=status, batch_id="b",
            username="u-prune", payload_hash="x",
            response_json={"tasks": [{"task_id": "t-1"}]},
            created_at=dt.datetime.now() - dt.timedelta(hours=age_hours)))
    return request_id


def test_prune_removes_old_done_and_stale_pending(_db):
    ensure_image_schema()
    old_done = _mk(f"rid-old-{uuid.uuid4().hex[:8]}", status="done", age_hours=100)
    fresh_done = _mk(f"rid-new-{uuid.uuid4().hex[:8]}", status="done", age_hours=1)
    stale_pending = _mk(f"rid-pend-{uuid.uuid4().hex[:8]}", status="pending", age_hours=5)
    fresh_pending = _mk(f"rid-pend2-{uuid.uuid4().hex[:8]}", status="pending", age_hours=0)

    removed = request_store.prune(keep_hours=72, pending_minutes=60)
    assert removed >= 2, removed

    with session_scope() as session:
        remaining = {row.request_id for row in session.query(ImageRequest).all()}
    assert old_done not in remaining, "过期 done 行必须清理"
    assert stale_pending not in remaining, "滞留 pending 行必须清理"
    assert fresh_done in remaining, "保留期内的幂等记录要留着（还要重放）"
    assert fresh_pending in remaining
