"""第九轮 P0-1：`usage.record` 的并发冲突回退不能回滚外层结算事务。

`usage.record()` 在结算事务内部调用（同事务里还有 mark_settled / 额度提交 / mark_done）。
旧实现用 `session.rollback()` 处理并发撞唯一索引 → 把**整个结算事务**抹掉：任务被打回
running、额度保持预留，随后看门狗重投造成重复出图与重复计费。
现在改为 SAVEPOINT（`begin_nested`）只回滚那一条 INSERT。
"""

from __future__ import annotations

import uuid

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents_image import settlement
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store.models import ImageQuota, ImageUsage
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


class _FakeConflict:
    """在「第一次插入 ImageUsage」时抛并发唯一冲突（模拟另一个事务先插入成功）。"""

    def __init__(self) -> None:
        self.fired = 0
        self._listener = self._before_flush

    def _before_flush(self, session, flush_context, instances):  # noqa: ANN001, ARG002
        if self.fired:
            return
        if any(isinstance(obj, ImageUsage) and getattr(obj, "id", None) is None
               for obj in session.new):
            self.fired += 1
            raise IntegrityError(
                "INSERT INTO image_usage (...) VALUES (...)", {},
                Exception("UNIQUE constraint failed: image_usage.username, "
                          "image_usage.period_key, image_usage.granularity"))

    def __enter__(self):
        event.listen(Session, "before_flush", self._listener)
        return self

    def __exit__(self, *exc):
        event.remove(Session, "before_flush", self._listener)
        return False


def _new_task(session, username: str, *, planned: int = 2) -> str:
    task_id = f"t-p01-{uuid.uuid4().hex[:10]}"
    task_store.create_task(
        session, task_id=task_id, username=username, department="女装",
        prompt="白底主图", platform="淘系", aspect="1:1", n=planned,
        ref_asset_ids=[], product_id="P-3001", image_count=planned,
    )
    return task_id


def test_conflict_fallback_keeps_outer_settlement_transaction(_db):
    """并发冲突时：settled_at / 额度提交 / 任务状态必须保留（旧实现会被整体回滚）。"""
    ensure_image_schema()
    username = f"u-p01-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        quota_store.grant(session, username, period_type="day", period_limit=10)

    with _FakeConflict() as conflict:
        with session_scope() as session:
            task_id = _new_task(session, username, planned=2)
            assert quota_store.reserve(session, username, 2)
            # 与 graph 交付路径同形：mark_done 与结算在**同一个事务**里
            task_store.mark_done(session, task_id, attempted=2, delivered=2, cost=0.4,
                                 external_cost=0.4)
            result = settlement.settle_done(
                session, task_id, delivered=2, attempted=2, cost=0.4)
            assert result["settled"] is True
    assert conflict.fired == 1, "冲突分支必须被触发"

    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        assert task.settled_at is not None, "结算权标记被回滚了（P0-1 复现）"
        assert task.status == "done"
        assert int(task.delivered_images or 0) == 2
        quota = session.get(ImageQuota, username)
        assert int(quota.used or 0) == 2, "交付张数必须转成已用额度"
        assert int(quota.reserved or 0) == 0, "预留必须释放/转正"


def test_conflict_fallback_does_not_raise(_db):
    """冲突回退路径必须吞掉唯一冲突继续结算（不能把结算打成失败）。"""
    ensure_image_schema()
    username = f"u-p01b-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        quota_store.grant(session, username, period_type="day", period_limit=5)
        task_id = _new_task(session, username, planned=1)
        assert quota_store.reserve(session, username, 1)

    with _FakeConflict():
        with session_scope() as session:
            settlement.settle_cancel(session, task_id, attempted=1, cost=0.2)

    with session_scope() as session:
        task = task_store.get_task(session, task_id)
        assert task.settled_at is not None, "取消结算同样不能被回滚"
