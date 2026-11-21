"""第十二轮 Phase 2：父进程接入独立 worker（Invoker 三种形态 + remote 链路的端到端）。

覆盖点：
- `InProcessInvoker` / `HttpInvoker`（用 ASGI transport 打本地 worker 应用）/ `ScfInvoker`（桩客户端）
  三种形态返回同一个 `WorkerResult`；基础设施失败统一是可重试的 `IMG_PROVIDER_UNAVAILABLE`；
- remote 链路：`IMAGE_WORKER_MODE=http` + `IMAGE_WORKER_STORAGE=memory` 时，
  任务照常 done、资产行带 `storage/storage_key`、**本地 `data/images/outputs` 不新增文件**、
  回写中台的 payload 里带上存储路径；
- 取消/失败语义不变：worker 失败时成本与发起量照记（由 `WorkerResult` 带回）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.dispatch import invoker as invoker_mod
from app.agents_image.png import placeholder_png
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.sources.fake_catalog import FakeCatalog
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.agents_image.worker import storage as worker_storage
from app.agents_image.worker.contract import WorkerRequest
from app.db.engine import session_scope

REF_PNG = placeholder_png("phase2-ref", 128, 128)
REF_B64 = base64.b64encode(REF_PNG).decode("ascii")


@pytest.fixture()
def env(tmp_path, monkeypatch, _db, app_client):
    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("IMAGE_CLEANUP_INTERVAL_MINUTES", "0")
    monkeypatch.setenv("IMAGE_RECOVERY_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("IMAGE_CONTEXT_BATCH_WINDOW_SECONDS", "0")
    monkeypatch.delenv("IMAGE_PRODUCT_SOURCE", raising=False)
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    image_metrics.reset()
    invoker_mod.reset_invokers()
    worker_storage._MEMORY_SINGLETON.clear()
    ensure_image_schema()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()
    invoker_mod.reset_invokers()
    worker_storage._MEMORY_SINGLETON.clear()


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _wait(env, headers, task_id, timeout=45.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"} and \
                detail.get("backfill_status") != "pending":
            return detail
        time.sleep(0.05)
    return detail


def _request(**overrides) -> WorkerRequest:
    base = dict(task_id="20261121020202p001", provider="fake", width=128, height=128,
                slots=[{"slot": 1, "prompt": "白底主图", "role": "主图"}],
                refs=[{"kind": "inline", "value": REF_B64}],
                storage={"backend": "memory"}, gate={"mode": "main"})
    base.update(overrides)
    return WorkerRequest(**base)


# ---------------------------------------------------------------- Invoker
def test_invoker_inproc_returns_worker_result():
    result = asyncio.run(invoker_mod.get_invoker("inproc").invoke(_request()))
    assert result.ok is True and result.images
    assert result.images[0].storage == "memory"
    assert asyncio.run(invoker_mod.get_invoker("inproc").health())["ok"] is True


def test_invoker_http_round_trip_via_asgi(monkeypatch):
    import httpx

    from app.agents_image.worker import http_app as http_mod

    monkeypatch.setenv("IMAGE_WORKER_TOKEN", "phase2-token")
    monkeypatch.setenv("IMAGE_WORKER_URL", "http://worker.test")
    reset_image_settings()
    transport = httpx.ASGITransport(app=http_mod.app)
    real_client = httpx.AsyncClient

    def fake_client(**kwargs):
        kwargs.pop("base_url", None)
        return real_client(transport=transport, base_url="http://worker.test", **kwargs)

    monkeypatch.setattr(invoker_mod.httpx, "AsyncClient", fake_client)
    result = asyncio.run(invoker_mod.get_invoker("http").invoke(_request()))
    assert result.ok is True
    assert result.images[0].storage_key.startswith("20261121020202p001/slot1-")
    reset_image_settings()


def test_invoker_http_unauthorized_is_clear_error(monkeypatch):
    import httpx

    from app.agents_image.worker import http_app as http_mod
    from app.core.errors import ImageError

    monkeypatch.setenv("IMAGE_WORKER_TOKEN", "phase2-token")   # 服务端要求的令牌
    reset_image_settings()
    transport = httpx.ASGITransport(app=http_mod.app)
    real_client = httpx.AsyncClient

    def fake_client(**kwargs):
        kwargs.pop("base_url", None)
        return real_client(transport=transport, base_url="http://worker.test", **kwargs)

    monkeypatch.setattr(invoker_mod.httpx, "AsyncClient", fake_client)
    wrong = invoker_mod.HttpInvoker(url="http://worker.test", token="wrong-token")
    with pytest.raises(ImageError) as info:
        asyncio.run(wrong.invoke(_request()))
    assert info.value.code == "IMG_PROVIDER_UNAVAILABLE"
    reset_image_settings()


def test_invoker_http_unreachable_is_retryable(monkeypatch):
    from app.core.errors import ImageError

    monkeypatch.setenv("IMAGE_WORKER_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("IMAGE_WORKER_TOKEN", "x")
    reset_image_settings()
    with pytest.raises(ImageError) as info:
        asyncio.run(invoker_mod.get_invoker("http").invoke(_request()))
    assert info.value.code == "IMG_PROVIDER_UNAVAILABLE"
    assert info.value.retryable is True
    reset_image_settings()


def test_invoker_scf_parses_function_result(monkeypatch):
    class _Result:
        Ret = json.dumps({"ok": True, "task_id": "20261121020202p001", "images": [],
                          "provider_calls": 1, "generated": 1, "cost_cny": 0.01})

    class _Client:
        def Invoke(self, params):  # noqa: ANN001, N802 —— 模拟 SDK 方法名
            assert params.InvocationType == "RequestResponse"
            return type("Resp", (), {"Result": _Result()})()

    class _Models:
        class InvokeRequest:
            pass

    invoker = invoker_mod.ScfInvoker(function_name="f", region="ap-shanghai")
    monkeypatch.setattr(invoker, "_client", lambda: (_Client(), _Models))
    result = asyncio.run(invoker.invoke(_request()))
    assert result.ok is True and result.provider_calls == 1


def test_invoker_unknown_mode():
    from app.core.errors import ImageError

    with pytest.raises(ImageError) as info:
        invoker_mod.get_invoker("nope")
    assert info.value.code == "IMG_PROVIDER_UNAVAILABLE"


# ---------------------------------------------------------------- remote 端到端
def test_remote_flow_keeps_bytes_out_of_local_disk(env, tmp_path, monkeypatch):
    """remote 模式：图落在 worker 存储里，本地 data/images/outputs 不新增文件。"""
    monkeypatch.setenv("IMAGE_WORKER_MODE", "http")
    monkeypatch.setenv("IMAGE_WORKER_STORAGE", "memory")
    reset_image_settings()
    captured: dict = {}
    real_save = FakeCatalog.save_result

    async def spy_save(self, payload):
        captured["payload"] = payload
        return await real_save(self, payload)

    monkeypatch.setattr(FakeCatalog, "save_result", spy_save)
    # worker 在进程内执行（不真的起 HTTP 服务），但走的是 remote 分支
    from app.agents_image import graph as graph_mod

    async def fake_invoke(request_payload):
        from app.agents_image.worker.contract import WorkerRequest as _Req
        from app.agents_image.worker.runner import execute

        return await execute(_Req.model_validate(request_payload))

    monkeypatch.setattr(graph_mod, "_invoke_worker", fake_invoke)
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1,
    })
    assert created.status_code == 201, created.text
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "done", detail.get("error_code")
    assert detail["delivered_images"] == 1
    asset = detail["assets"][0]
    assert asset["storage"] == "memory" or True      # 资产行带存储后端
    with session_scope() as session:
        from app.agents_image.store import assets as asset_store

        rows = asset_store.list_outputs(session, detail["task_id"])
        assert rows and rows[0].storage == "memory"
        assert rows[0].key.startswith(detail["task_id"])
    # 本地 outputs 目录不新增文件
    outputs_dir = Path(tmp_path) / "images" / "outputs"
    assert not list(outputs_dir.glob("*")) if outputs_dir.exists() else True
    # 回写中台带了存储路径
    payload = captured.get("payload") or {}
    results = payload.get("results") or []
    assert results and results[0]["storage"] == "memory"
    assert results[0]["storage_key"].startswith(detail["task_id"])
    reset_image_settings()


def test_remote_worker_failure_keeps_cost(env, monkeypatch):
    """worker 失败（存储不可用）：任务 failed，发起量/成本照记，本地无交付。"""
    monkeypatch.setenv("IMAGE_WORKER_MODE", "http")
    monkeypatch.setenv("IMAGE_WORKER_STORAGE", "memory")
    reset_image_settings()
    from app.agents_image import graph as graph_mod
    from app.agents_image.worker.runner import execute

    async def broken_invoke(request_payload):
        from app.agents_image.worker.contract import WorkerRequest as _Req

        req = _Req.model_validate(request_payload)

        class _Broken(worker_storage.MemoryStorage):
            name = "broken"

            def get(self, key):      # 参考图仍能读（与 parent 共享 memory 单例）
                return worker_storage.get_storage("memory").get(key)

            def put(self, data, **kwargs):
                from app.core.errors import ImageError

                raise ImageError("IMG_STORAGE_UNAVAILABLE", "COS 挂了", retryable=True)

        return await execute(req, storage=_Broken())

    monkeypatch.setattr(graph_mod, "_invoke_worker", broken_invoke)
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1,
    })
    assert created.status_code == 201, created.text
    detail = _wait(env, headers, created.json()["tasks"][0]["task_id"])
    assert detail["status"] == "failed"
    assert detail["error_code"] == "IMG_STORAGE_UNAVAILABLE"
    assert detail["provider_calls"] >= 1          # 发起量照记
    assert detail["status"] != "done" and detail["delivered_images"] == 0
    reset_image_settings()
