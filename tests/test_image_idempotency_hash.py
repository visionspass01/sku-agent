"""第九轮 P1-3：幂等键必须绑定请求体（同 key 不同参数 → 409，而不是重放旧响应）。

上一版只看 `Idempotency-Key` / body.request_id：客户端复用同一个 key 改参数重发时，
会被静默重放成**旧响应**——用户以为新参数生效，实际一批任务都没重建，
排查时账实不符。
"""

from __future__ import annotations

import uuid

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import tasks as task_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    image_metrics.reset()
    ensure_image_schema()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _stub_dispatcher(monkeypatch) -> None:
    class _Stub:
        async def submit(self, task_id: str) -> str:
            return task_id

    monkeypatch.setattr("app.agents_image.api.routes.get_dispatcher", lambda: _Stub())


def _post(env, headers, *, request_id: str, image_count: int, prompt: str = "白底主图"):
    return env.post("/api/image/tasks", headers=headers,
                    json={"request_id": request_id, "product_ids": ["P-3001"],
                          "prompt": prompt, "image_count": image_count})


def test_same_key_same_body_replays(env, monkeypatch):
    _stub_dispatcher(monkeypatch)
    headers = login(env)
    rid = f"rid-{uuid.uuid4().hex[:10]}"

    first = _post(env, headers, request_id=rid, image_count=1)
    assert first.status_code == 201, first.text
    second = _post(env, headers, request_id=rid, image_count=1)
    assert second.status_code == 200, second.text
    assert second.json()["idempotent"] is True
    assert second.json()["batch_id"] == first.json()["batch_id"]


def test_same_key_different_body_is_rejected(env, monkeypatch):
    """同 key 换参数必须 409，且不能新建任务（否则会重复扣额度/重复出图）。"""
    _stub_dispatcher(monkeypatch)
    headers = login(env)
    user = "admin"
    rid = f"rid-{uuid.uuid4().hex[:10]}"

    first = _post(env, headers, request_id=rid, image_count=1)
    assert first.status_code == 201, first.text
    with session_scope() as session:
        count_before = len(task_store.list_tasks(session, username=user, limit=500))

    changed = _post(env, headers, request_id=rid, image_count=2, prompt="换一个需求")
    assert changed.status_code == 409, changed.text
    assert changed.json()["error_code"] == "IMG_TASK_CONFLICT"
    assert "不同参数" in changed.json()["detail"]

    with session_scope() as session:
        count_after = len(task_store.list_tasks(session, username=user, limit=500))
    assert count_after == count_before, "冲突请求不得再建任务"


def test_idempotency_key_header_is_also_bound(env, monkeypatch):
    _stub_dispatcher(monkeypatch)
    headers = login(env)
    rid = f"hdr-{uuid.uuid4().hex[:10]}"

    def post_with_header(image_count: int):
        return env.post("/api/image/tasks",
                        headers={**headers, "Idempotency-Key": rid},
                        json={"product_ids": ["P-3001"], "prompt": "白底主图",
                              "image_count": image_count})

    assert post_with_header(1).status_code == 201
    assert post_with_header(1).status_code == 200                    # 同样的请求 → 重放
    assert post_with_header(2).status_code == 409                    # 参数变了 → 冲突
