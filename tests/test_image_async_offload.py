"""第十轮 P1-3：async 端点里的同步 DB/IO 不能占事件循环。

认证（每请求 2 次 SELECT）、建任务的写事务、采纳/驳回、上传、重试、SSE 首帧快照——
这些同步块统一走线程池（`run_in_trace`）。测试用"线程 id"证明它们不在事件循环线程里跑。
"""

from __future__ import annotations

import threading

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.api import deps, routes
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


def _spy_loop_thread(monkeypatch) -> dict:
    """`trace_id_of` 在路由函数里同步调用 → 它的线程就是事件循环线程。"""
    idents: dict[str, int] = {}
    real = routes.trace_id_of

    def spy(request):
        idents.setdefault("loop", threading.get_ident())
        return real(request)

    monkeypatch.setattr(routes, "trace_id_of", spy)
    return idents


def test_auth_and_write_txn_run_off_the_event_loop(env, monkeypatch):
    """认证（线程池）与建任务写事务（线程池）都不得在事件循环线程里跑。"""
    _stub_dispatcher(monkeypatch)
    idents = _spy_loop_thread(monkeypatch)

    real_load_user = deps._load_user

    def spy_load_user(request):
        idents["auth"] = threading.get_ident()
        return real_load_user(request)

    monkeypatch.setattr(deps, "_load_user", spy_load_user)

    real_create = task_store.create_task

    def spy_create(*args, **kwargs):
        idents["create"] = threading.get_ident()    # 写事务在线程池里执行
        return real_create(*args, **kwargs)

    monkeypatch.setattr(task_store, "create_task", spy_create)

    headers = login(env)
    resp = env.post("/api/image/tasks", headers=headers,
                    json={"product_ids": ["P-3001"], "prompt": "白底主图",
                          "image_count": 1})
    assert resp.status_code == 201, resp.text
    assert idents["auth"] != idents["loop"], "认证必须在事件循环之外执行"
    assert idents["create"] != idents["loop"], "建任务写事务必须在事件循环之外执行"


def test_adopt_offloads_feedback_write(env, monkeypatch):
    """采纳的同步 DB 写（反馈行 + 使用量 shift）不得占事件循环。"""
    _stub_dispatcher(monkeypatch)
    idents = _spy_loop_thread(monkeypatch)
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图",
                             "image_count": 1})
    task_id = created.json()["tasks"][0]["task_id"]
    from app.agents_image.png import placeholder_png
    from app.agents_image.store import assets as asset_store

    with session_scope() as session:
        asset = asset_store.save_bytes(session, placeholder_png("adopt", 48, 48),
                                       kind="output", task_id=task_id, slot=1,
                                       username="admin", product_id="P-3001")
        asset_id = asset.asset_id

    from app.agents_image.store import feedback as feedback_store

    real_record = feedback_store.record

    def spy_record(*args, **kwargs):
        idents["feedback"] = threading.get_ident()
        return real_record(*args, **kwargs)

    monkeypatch.setattr(feedback_store, "record", spy_record)
    resp = env.post(f"/api/image/tasks/{task_id}/assets/{asset_id}/adopt",
                    headers=headers)
    assert resp.status_code == 200, resp.text
    assert idents["feedback"] != idents["loop"], "采纳的同步 DB 写必须在线程池里执行"
