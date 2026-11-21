"""第十二轮复审修复的回归用例（P1-1…P1-11 + P2-1…P2-8）。

每条用例对应报告里的一条编号，命名保持 `test_p1_x_*` / `test_p2_x_*`，方便按编号回查。
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import LoginThrottle, reset_throttle
from app.agents_image.config import get_image_settings, reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.sources import refs as refs_source
from app.agents_image.sources.product_source import ProductData, ProductImage, ProductSku
from app.agents_image.store import quota as quota_store
from app.agents_image.store import tasks as task_store
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.core.errors import ImageError, normalize
from app.db.engine import session_scope


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
    ensure_image_schema()
    with session_scope() as session:
        user_store.ensure_seed_users(session)
    yield app_client
    reset_image_settings()
    reset_throttle()
    ps.reset_catalogs()
    ps._ensure_builtin()
    batch_context.reset_product_batcher()


@pytest.fixture()
def db_only(_db):
    """只要建表（不需要 HTTP client 的用例）。"""
    ensure_image_schema()
    return None


def login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _stub_dispatcher(monkeypatch) -> None:
    class _Stub:
        async def submit(self, task_id: str) -> str:
            return task_id

    monkeypatch.setattr("app.agents_image.api.routes.get_dispatcher", lambda: _Stub())


def _loop_thread(_client) -> tuple[int, int]:
    """返回 (事件循环线程 id, 当前线程 id)：用一次同步端点标定 loop 线程。"""
    from app.agents_image.api import routes

    ident = {"loop": 0}
    real = routes.trace_id_of

    def spy(request):
        ident.setdefault("loop", threading.get_ident())
        return real(request)

    routes.trace_id_of = spy  # noqa: SLF001 —— 只在用例内临时替换
    return ident["loop"], threading.get_ident()


# ---------------------------------------------------------------- P1-1
def test_p1_1_retry_batch_offloads_auth_and_read(env, monkeypatch):
    """整批重试的认证与批次读取都必须在线程池里执行（不在事件循环线程）。"""
    _stub_dispatcher(monkeypatch)
    from app.agents_image.api import batch_routes, deps

    ids: dict[str, int] = {}
    real_require = batch_routes.require_user_async

    async def spy_require(request):
        ids["loop"] = threading.get_ident()      # 协程体 = 事件循环线程
        return await real_require(request)

    monkeypatch.setattr(batch_routes, "require_user_async", spy_require)

    real_load = deps._load_user

    def spy_load(request):
        ids.setdefault("auth", threading.get_ident())
        return real_load(request)

    monkeypatch.setattr(deps, "_load_user", spy_load)

    real_tasks = batch_routes._batch_tasks

    def spy_tasks(session, batch_id):
        ids["read"] = threading.get_ident()
        return real_tasks(session, batch_id)

    monkeypatch.setattr(batch_routes, "_batch_tasks", spy_tasks)

    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1})
    batch_id = created.json()["batch_id"]
    resp = env.post("/api/image/tasks/retry-batch", headers=headers, json={"batch_id": batch_id})
    assert resp.status_code == 200, resp.text
    assert ids["auth"] != ids["loop"], "认证必须在线程池里执行"
    assert ids["read"] != ids["loop"], "批次读取必须在线程池里执行"


# ---------------------------------------------------------------- P1-2
def test_p1_2_sse_poll_runs_off_loop_thread(env):
    """SSE 轮询的同步 DB 必须在线程池里执行（不在事件循环线程）。"""
    from app.agents_image.api import stream_routes

    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1})
    task_id = created.json()["tasks"][0]["task_id"]
    with session_scope() as session:
        task_store.mark_done(session, task_id, attempted=1, delivered=1, cost=0.0)
        snapshot = stream_routes._snapshot_payload(task_store.get_task(session, task_id))

    seen: list[int] = []
    real_get = task_store.get_task

    def spy_get(session, tid):
        seen.append(threading.get_ident())
        return real_get(session, tid)

    stream_routes.task_store.get_task = spy_get
    try:
        async def drive() -> list[str]:
            events = []
            async for chunk in stream_routes._stream_body(task_id, snapshot, "trace-x"):
                events.append(chunk)
                if len(events) >= 2:
                    break
            return events

        asyncio.run(drive())
    finally:
        stream_routes.task_store.get_task = real_get
    assert seen, "轮询没有读库"
    assert seen[0] != threading.get_ident(), "轮询必须在工作线程执行，不能占事件循环"


# ---------------------------------------------------------------- P1-3
def test_p1_3_renew_lease_is_async_and_offloads():
    from app.agents_image import graph

    assert asyncio.iscoroutinefunction(graph._renew_lease)
    assert asyncio.iscoroutinefunction(graph._gate_image)


def test_p1_3_submit_claim_offloaded(env):
    """提交任务时的 CAS 认领不能压在事件循环线程上。"""
    from app.agents_image.dispatch.local_generator import LocalGenerator

    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1})
    task_id = created.json()["tasks"][0]["task_id"]

    seen: list[int] = []
    real_claim = task_store.claim

    def spy_claim(session, tid, **kwargs):
        seen.append(threading.get_ident())
        return real_claim(session, tid, **kwargs)

    task_store.claim = spy_claim
    try:
        asyncio.run(LocalGenerator().submit(task_id))
    finally:
        task_store.claim = real_claim
    assert seen and seen[0] != threading.get_ident()


# ---------------------------------------------------------------- P1-4
def test_p1_4_waiter_gets_error_instead_of_hanging(monkeypatch):
    """合并任务异常退出时，等待中的请求必须收到错误，而不是永久挂在 future 上。"""
    monkeypatch.setenv("IMAGE_CONTEXT_BATCH_WINDOW_SECONDS", "0")
    monkeypatch.setenv("IMAGE_CONTEXT_BATCH_TIMEOUT_SECONDS", "5")
    reset_image_settings()

    class _Boom:
        name = "boom"

        async def get_products_context(self, ids):
            raise RuntimeError("中台挂了")

    batcher = batch_context.ProductContextBatcher(_Boom())

    async def scenario():
        return await asyncio.wait_for(batcher.get_many(["P-1001"]), timeout=3)

    with pytest.raises(ImageError) as info:
        asyncio.run(scenario())
    # 上游异常统一收敛成 503（retryable），不能变成 500
    assert info.value.code == "IMG_UPSTREAM_MCP_UNAVAILABLE"
    assert info.value.retryable is True
    reset_image_settings()


def test_p1_4_timeout_budget_fails_fast(monkeypatch):
    """取数超过预算必须报错（retryable），而不是无限等待。"""
    monkeypatch.setenv("IMAGE_CONTEXT_BATCH_WINDOW_SECONDS", "0")
    reset_image_settings()
    monkeypatch.setattr(batch_context, "_budget_seconds", lambda: 1.0)

    class _Slow:
        name = "slow"

        async def get_products_context(self, ids):
            await asyncio.sleep(3)
            return {}

    batcher = batch_context.ProductContextBatcher(_Slow())
    started = time.monotonic()
    with pytest.raises(ImageError) as info:
        asyncio.run(batcher.get_many(["P-1001"]))
    assert info.value.code == "IMG_UPSTREAM_MCP_UNAVAILABLE"
    assert info.value.retryable is True
    assert time.monotonic() - started < 2.5
    reset_image_settings()


# ---------------------------------------------------------------- P1-5
def test_p1_5_mark_running_refuses_canceled_task(db_only):
    """已取消的任务不能被改回 running；mark_failed 也不能覆盖 canceled。"""
    with session_scope() as session:
        task_store.create_task(session, task_id="20261120000000cc01", username="worker1",
                               department="女装", prompt="白底主图", platform="淘系",
                               aspect="1:1", n=1, ref_asset_ids=[], product_id="P-1001",
                               image_count=1)
        task_store.update_task(session, "20261120000000cc01", status="canceled")

    with session_scope() as session:
        assert task_store.mark_running(session, "20261120000000cc01") is False
    with session_scope() as session:
        task = task_store.get_task(session, "20261120000000cc01")
        assert task.status == "canceled"
        assert task_store.mark_failed(session, "20261120000000cc01", code="IMG_TIMEOUT") is False
    with session_scope() as session:
        assert task_store.get_task(session, "20261120000000cc01").status == "canceled"


def test_p1_5_mark_running_only_from_queued(db_only):
    with session_scope() as session:
        task_store.create_task(session, task_id="20261120000000cc02", username="worker1",
                               department="女装", prompt="白底主图", platform="淘系",
                               aspect="1:1", n=1, ref_asset_ids=[], product_id="P-1001",
                               image_count=1)
    with session_scope() as session:
        assert task_store.mark_running(session, "20261120000000cc02") is True
    with session_scope() as session:
        task = task_store.get_task(session, "20261120000000cc02")
        assert task.status == "running"
        assert task.started_at is not None


# ---------------------------------------------------------------- P1-6
def test_p1_6_task_id_collision_is_retried(db_only):
    """主键撞车只回滚这一条 INSERT，换 id 重试，外层事务不受影响。"""
    ids = iter(["20261120000000dd01", "20261120000000dd01", "20261120000000dd02"])
    with session_scope() as session:
        task_store.create_task(session, task_id="20261120000000dd01", username="worker1",
                               department="女装", prompt="占位", platform="淘系",
                               aspect="1:1", n=1, ref_asset_ids=[], product_id="P-1001",
                               image_count=1)
        task = task_store.create_task_with_retry(
            session, id_factory=lambda: next(ids), username="worker1", department="女装",
            prompt="白底主图", platform="淘系", aspect="1:1", n=1,
            ref_asset_ids=[], product_id="P-1001", image_count=1)
        assert task.task_id == "20261120000000dd02"
        # 同一事务后续写入仍然有效（没有被整体回滚）
        task_store.update_task(session, "20261120000000dd01", error_detail="still-alive")
    with session_scope() as session:
        assert task_store.get_task(session, "20261120000000dd01").error_detail == "still-alive"
        assert task_store.get_task(session, "20261120000000dd02") is not None


# ---------------------------------------------------------------- P1-7
def test_p1_7_due_rows_not_starved_by_backoff_rows(db_only):
    """最老的 pending 还在退避期时，真正到期的行也必须能被重放（SQL 侧过滤）。"""
    import datetime as dt

    from app.agents_image.store import outbox as outbox_store

    now = dt.datetime.now()
    with session_scope() as session:
        backoff = outbox_store.ImageOutbox(
            task_id="t-backoff", payload_json={"a": 1}, status="pending", attempts=3,
            next_retry_at=now + dt.timedelta(hours=1),
            created_at=now - dt.timedelta(minutes=30))
        due = outbox_store.ImageOutbox(
            task_id="t-due", payload_json={"b": 2}, status="pending", attempts=0,
            next_retry_at=now - dt.timedelta(seconds=5),
            created_at=now - dt.timedelta(minutes=1))
        session.add_all([backoff, due])
        session.flush()
    rows = outbox_store.pending_payloads(limit=1)
    # 整个测试会话共用一份 sqlite（tests/conftest.py 的 `_db` 是 session 级），别的用例
    # 可能留下"到期"的行——例如 claim 的 600s 占位窗口在长跑（本地 Windows 全套近 50 分钟）
    # 里过期后会重新变成可重放。所以这里只锁 P1-7 的回归点：
    # 「最老的 pending 还在退避期时，到期行不能被饿死」，而不假定自己是唯一一行。
    assert rows, "到期行一条都没捞到"
    assert rows[0]["task_id"] != "t-backoff", (
        f"退避期的老行把到期行饿死了（P1-7 回归）：{rows[0]['task_id']}")
    due_rows = {row["task_id"] for row in outbox_store.pending_payloads(limit=500)}
    assert "t-due" in due_rows
    assert "t-backoff" not in due_rows


# ---------------------------------------------------------------- P1-8
def test_p1_8_refs_download_concurrently(monkeypatch):
    """多张参考图并发下载（不是逐张串行）。

    单张 0.4s：串行要 ≥1.6s、并发只要 ~0.4s。门槛取 0.8s（串行的一半），
    这样在共享 runner 上多几百毫秒抖动也不会误判——历史上这里用 0.15s×4 + <0.45s，
    抖动一大就假红。
    """
    sleeps = {"n": 0}

    class _Catalog:
        name = "refs-conc"

        async def get_product(self, product_id):
            return ProductData(
                product_id=product_id, title="商品", category="女装",
                attrs={"color_names": ["米色"]},
                images=[ProductImage(image_id=f"{product_id}-{i}", color="米色",
                                     color_hex="#C8B7A6", bytes_b64="aGVsbG8=")
                        for i in range(4)],
                skus=[ProductSku(sku_id=f"{product_id}-米色", color="米色",
                                 color_hex="#C8B7A6")])

        async def fetch_image_bytes(self, image):
            sleeps["n"] += 1
            await asyncio.sleep(0.4)
            return b"payload"

    catalog = _Catalog()
    targets = [refs_source.RefTarget(product_id="P-1001", color="米色",
                                     color_hex="#C8B7A6")]
    started = time.monotonic()
    plan = asyncio.run(refs_source.plan_refs(catalog, targets, refs_per_item=4, max_refs=4))
    elapsed = time.monotonic() - started
    assert plan.total == 4
    assert sleeps["n"] == 4
    assert elapsed < 0.8, f"仍是串行下载（{elapsed:.2f}s；串行应 ≥1.6s，并发约 0.4s）"
    assert [item.image_id for item in plan.refs_of("P-1001#米色")] == [
        "P-1001-0", "P-1001-1", "P-1001-2", "P-1001-3"]


def test_p1_8_refs_download_honours_budget(monkeypatch):
    monkeypatch.setenv("IMAGE_REFS_DOWNLOAD_BUDGET_SECONDS", "1")
    reset_image_settings()

    class _SlowCatalog:
        name = "refs-slow"

        async def get_product(self, product_id):
            return ProductData(
                product_id=product_id, title="商品", category="女装",
                attrs={"color_names": ["米色"]},
                images=[ProductImage(image_id="i1", color="米色", color_hex="#C8B7A6",
                                     bytes_b64="aGVsbG8=")],
                skus=[ProductSku(sku_id="s1", color="米色", color_hex="#C8B7A6")])

        async def fetch_image_bytes(self, image):
            await asyncio.sleep(3)
            return b"payload"

    targets = [refs_source.RefTarget(product_id="P-1001", color="米色", color_hex="#C8B7A6")]
    with pytest.raises(ImageError) as info:
        asyncio.run(refs_source.plan_refs(_SlowCatalog(), targets, refs_per_item=1, max_refs=2))
    assert info.value.code == "IMG_UPSTREAM_MCP_UNAVAILABLE"
    assert info.value.retryable is True
    reset_image_settings()


# ---------------------------------------------------------------- P1-10
def test_p1_10_unknown_exception_is_sanitized():
    """未知异常不能把原始异常串（SQL/路径）回给调用方。"""
    err = normalize(RuntimeError("UNIQUE constraint failed: image_task.task_id"), "trace-1")
    assert err.code == "IMG_INTERNAL"
    assert "UNIQUE" not in err.detail and "image_task" not in err.detail
    assert err.trace_id == "trace-1"
    # 业务错误原样透传
    biz = normalize(ImageError("IMG_INPUT_INVALID", "参数不对"), "trace-2")
    assert biz.detail == "参数不对"


# ---------------------------------------------------------------- P1-11
def test_p1_11_cross_period_settle_still_charges(db_only):
    """跨周期结算：预留随周期清零后，已交付张数必须记进当期 used（不能免扣）。"""
    from app.agents_image import settlement

    with session_scope() as session:
        quota_store.grant(session, "u-cross", period_type="day", period_limit=50)
        assert quota_store.reserve(session, "u-cross", 3) is True
        task_store.create_task(session, task_id="20261120000000ee01", username="u-cross",
                               department="女装", prompt="白底主图", platform="淘系",
                               aspect="1:1", n=3, ref_asset_ids=[], product_id="P-1001",
                               image_count=3)
        # 模拟"跨天"：把周期键改回昨天，让 _rollover 在结算时清空 reserved
        row = quota_store.get_or_create(session, "u-cross")
        row.period_key = "1999-01-01"
        session.flush()

    warnings: list[str] = []
    with session_scope() as session:
        result = settlement.settle_done(session, "20261120000000ee01", delivered=3,
                                        attempted=3, cost=0.0, warnings=warnings)
    assert result["settled"] is True
    with session_scope() as session:
        row = quota_store.get_or_create(session, "u-cross")
        assert int(row.used or 0) == 3, "跨周期后仍必须扣减"
        assert int(row.reserved or 0) == 0
    assert not [item for item in warnings if item.startswith("IMG_QUOTA_SETTLE_MISMATCH")]


# ---------------------------------------------------------------- P2-1
def test_p2_1_decoded_pixels_are_reused(monkeypatch, db_only):
    """Critic 与像素门禁复用同一份解码结果（不再各解一遍）。"""
    from app.agents_image import pixels
    from app.agents_image.nodes import compliance
    from app.agents_image.nodes import critic as critic_mod
    from app.agents_image.png import placeholder_png

    data = placeholder_png("reuse", 64, 64)
    decoded = pixels.decode_any(data)
    assert decoded is not None

    calls = {"n": 0}
    real_decode = pixels.decode_any

    def spy_decode(raw):
        calls["n"] += 1
        return real_decode(raw)

    monkeypatch.setattr(critic_mod, "decode_any", spy_decode)
    monkeypatch.setattr(compliance, "decode_any", spy_decode)
    image = critic_mod.CheckedImage(slot=1, data=data, mime="image/png")
    verdict = compliance.gate(data, slot=1, role="主图", gate_mode="all", decoded=decoded)
    assert verdict.stats.get("pixel_check") is True
    state = critic_mod.ImageTaskState(prompt="白底主图", aspect="1:1", count=1,
                                      attrs={"color_hex": "#FFFFFF"})
    critic_mod.critique(state, [], [image], settings=get_image_settings(),
                        decoded=[decoded])
    assert calls["n"] == 0, "传了 decoded 还去解码，等于没复用"


# ---------------------------------------------------------------- P2-2
def test_p2_2_health_reports_real_in_flight_and_pending(env, monkeypatch):
    from app.agents_image import runtime

    _stub_dispatcher(monkeypatch)          # 任务停在 queued，pending 才有稳定值
    runtime._PENDING_CACHE.clear()
    headers = login(env)
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1})
    assert created.status_code == 201, created.text
    runtime._PENDING_CACHE.clear()
    block = runtime.health_block()
    assert block["tasks_pending"] >= 1, "排队中的任务必须在 health 里可见"
    assert isinstance(block["in_flight_images"], int)


# ---------------------------------------------------------------- P2-3
def test_p2_3_batch_ops_and_task_list_are_rate_limited(env, monkeypatch):
    monkeypatch.setenv("IMAGE_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("IMAGE_RATE_LIMIT_PER_MINUTE", "1")
    reset_image_settings()
    headers = login(env)
    first = env.get("/api/image/tasks", headers=headers)
    second = env.get("/api/image/tasks", headers=headers)
    assert first.status_code == 200, first.text
    assert second.status_code == 429, second.text
    assert second.json()["error_code"] == "IMG_RATE_LIMITED"
    reset_image_settings()


# ---------------------------------------------------------------- P2-4
def test_p2_4_login_throttle_prunes_stale_keys():
    throttle = LoginThrottle(max_attempts=3, window_seconds=60)
    stale = 1000.0
    for index in range(600):
        throttle.record_failure(f"user{index}", "1.1.1.1", now=stale)
    throttle.allowed("user0", "1.1.1.1", now=stale + 120)   # 触发一次清理
    assert len(throttle._failures) < 600


# ---------------------------------------------------------------- P2-5
def test_p2_5_catalog_reads_run_concurrently(env, monkeypatch):
    """/catalog 的四个上游调用必须**并发**发出（用峰值并发数判定，不依赖墙钟）。"""
    import asyncio

    from app.agents_image.sources.fake_catalog import FakeCatalog

    class _ProbeCatalog(FakeCatalog):
        name = "probe-catalog"

        def __init__(self) -> None:
            super().__init__()
            self.in_flight = 0
            self.peak = 0

        async def _run(self, call):  # noqa: ANN001, ANN202
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            try:
                await asyncio.sleep(0.05)
                return await call()
            finally:
                self.in_flight -= 1

        async def get_platform_rules(self, *, platform="淘系", **kwargs):  # noqa: ANN001
            return await self._run(lambda: super(_ProbeCatalog, self)
                                   .get_platform_rules(platform=platform))

        async def get_plan_policy(self, *, platform="淘系", category="", **kwargs):  # noqa: ANN001
            return await self._run(lambda: super(_ProbeCatalog, self)
                                   .get_plan_policy(platform=platform, category=category))

        async def list_compositions(self, *, category="", platform="淘系", **kwargs):  # noqa: ANN001
            return await self._run(lambda: super(_ProbeCatalog, self)
                                   .list_compositions(category=category, platform=platform))

        async def list_scene_styles(self, *, category="", platform="淘系", **kwargs):  # noqa: ANN001
            return await self._run(lambda: super(_ProbeCatalog, self)
                                   .list_scene_styles(category=category, platform=platform))

    catalog = _ProbeCatalog()
    from app.agents_image.api import routes as routes_mod

    monkeypatch.setattr(routes_mod, "get_catalog", lambda *a, **k: catalog)
    headers = login(env)
    resp = env.get("/api/image/catalog", headers=headers)
    assert resp.status_code == 200, resp.text
    assert catalog.peak >= 2, f"四个上游调用仍是串行（峰值并发 {catalog.peak}）"


def test_p2_6_library_limit_is_enforced_atomically(env, monkeypatch):
    from app.agents_image.png import placeholder_png
    from app.agents_image.store import assets as asset_store

    monkeypatch.setenv("IMAGE_LIBRARY_MAX_IMAGES", "1")
    reset_image_settings()
    headers = login(env, "worker1", "hhw111")
    import base64

    def _payload(seed: str) -> dict:
        return {"data_b64": base64.b64encode(placeholder_png(seed, 32, 32)).decode()}

    first = env.post("/api/image/library", headers=headers, json=_payload("a"))
    assert first.status_code == 201, first.text
    second = env.post("/api/image/library", headers=headers, json=_payload("b"))
    assert second.status_code == 422, second.text
    assert second.json()["error_code"] == "IMG_LIBRARY_FULL"
    with session_scope() as session:
        assert asset_store.count_library(session, "worker1") == 1
    reset_image_settings()


# ---------------------------------------------------------------- P2-7
def test_p2_7_local_store_writes_atomically(tmp_path, monkeypatch):
    from app.agents_image.storage import local_store

    monkeypatch.setenv("IMAGE_IMAGES_ROOT", str(tmp_path / "images"))
    reset_image_settings()
    store = local_store.LocalStore()
    monkeypatch.setattr(local_store, "image_size", lambda raw: (1, 1))
    data = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    replaced: list[str] = []
    real_replace = local_store.os.replace

    def spy_replace(src, dst):  # noqa: ANN001 —— 记录是否走原子替换
        replaced.append(str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(local_store.os, "replace", spy_replace)
    ref = store.put(data, kind="ref", mime="")
    assert replaced, "必须用 os.replace 原子落盘，不能直接写目标文件"
    assert store.get(ref.key) == data                                    # 内容可原样读回
    assert not list((tmp_path / "images" / "refs").glob("*.tmp"))        # 临时文件不残留
    reset_image_settings()


# ---------------------------------------------------------------- P2-8
def test_p2_8_local_rag_cache_honours_ttl(monkeypatch):
    from app.agents_image.rag import index as rag_index

    monkeypatch.setattr(rag_index, "_shared_cache", lambda: None)
    monkeypatch.setattr(rag_index, "_cache_ttl", lambda: 60)
    key = ("fp", "q", 3, "", "", 0.0, "heuristic")
    rag_index._cache_set(key, [{"doc_id": "d1"}])
    assert rag_index._cache_get(key) is not None
    rag_index._LOCAL_CACHE[key]["expire_at"] = 0.0
    assert rag_index._cache_get(key) is None, "过期的本地缓存必须失效"
    assert key not in rag_index._LOCAL_CACHE
