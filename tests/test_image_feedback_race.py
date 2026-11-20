"""第九轮 P2-9：并发双击采纳不能 500、也不能重复计数。

`ImageFeedback` 有 `uq_image_feedback_asset`（asset_id 唯一）；两个并发 adopt 请求
（前端连点/双开）后到者会撞唯一索引——上一版直接抛 IntegrityError → 500，
而且两者都读到 previous=""，即使写成功也会各 shift 一次 `adopted`（重复计数）。
现在后到者退回"覆盖更新"，并以胜者写入的状态重新计算 changed。
"""

from __future__ import annotations

import threading
import uuid

from app.agents_image.png import placeholder_png
from app.agents_image.store import assets as asset_store
from app.agents_image.store import feedback as feedback_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import usage as usage_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


def _setup() -> tuple[str, str]:
    ensure_image_schema()
    user = f"u-flip-{uuid.uuid4().hex[:8]}"
    task_id = f"t-flip-{uuid.uuid4().hex[:10]}"
    with session_scope() as session:
        task_store.create_task(session, task_id=task_id, username=user, department="女装",
                               prompt="白底主图", platform="淘系", aspect="1:1", n=1,
                               ref_asset_ids=[], product_id="P-3001")
        asset = asset_store.save_bytes(session, placeholder_png("flip", 48, 48),
                                       kind="output", task_id=task_id, slot=1,
                                       username=user, product_id="P-3001")
        usage_store.record(session, username=user, delivered=1, attempted=1,
                           tasks=1, agent_passed=1)
        return task_id, asset.asset_id


def test_concurrent_adopt_is_idempotent_not_500(_db):
    task_id, asset_id = _setup()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    ok = {"n": 0}

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            with session_scope() as session:
                feedback_store.record(session, task_id=task_id, asset_id=asset_id,
                                      verdict="adopted", username="admin", slot=1)
            ok["n"] += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert ok["n"] == 2, "两个并发请求都必须成功返回（不能 500）"
    with session_scope() as session:
        username = task_store.get_task(session, task_id).username
        mine = usage_store.totals(session, username=username)
        status = asset_store.get_asset(session, asset_id).adopt_status
    assert mine["adopted"] == 1, mine
    assert status == "adopted"
