"""第四轮 W2：采纳口径（P1-1 上架率 ≤100%、P1-2 终态记账、P2-13 拦截图必须先放行）。

核心口径（与 CONTEXT_PACK §6 裁决一致）：
- 上架率 = 新生成图采纳 / 使用量；复用图采纳单独记 `reused_adopted`（只做 hint）；
- 采纳按**当前终态**记账：adopt→reject→adopt 只算 1 次采纳，不 flip-flop 虚增；
- 被门禁拦下的图（kind=blocked）不能直接采纳，必须先 unblock 人工放行。

注意：测试库是 session 级的（`_db`），所以计数断言一律用「本用例专属账号」或前后差量，
不能对全局 totals 写绝对值。
"""

from __future__ import annotations

import time
import uuid

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.png import placeholder_png
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import assets as asset_store
from app.agents_image.store import feedback as feedback_store
from app.agents_image.store import usage as usage_store
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


def _wait(env, headers, task_id, timeout=25.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = env.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"} and \
                detail.get("backfill_status") != "pending":
            return detail
        time.sleep(0.05)
    return detail


def _cards(env, headers, username: str | None = None) -> dict:
    query = "granularity=day&count=7"
    if username:
        query += f"&username={username}"
    return env.get(f"/api/image/admin/metrics?{query}&refresh=1", headers=headers).json()["cards"]


def _done_task(env, headers, product="P-3001") -> dict:
    created = env.post("/api/image/tasks", headers=headers,
                       json={"product_ids": [product], "prompt": "白底主图", "image_count": 1})
    assert created.status_code == 201, created.text
    task_id = created.json()["tasks"][0]["task_id"]
    detail = _wait(env, headers, task_id)
    assert detail["status"] == "done", detail
    detail["task_id"] = task_id
    return detail


def _make_asset(session, *, kind, task_id=None, source="mcp", slot=1, username="admin"):
    data = placeholder_png(f"{kind}-{source}-{uuid.uuid4().hex[:6]}", 48, 48)
    return asset_store.save_bytes(session, data, kind=kind, mime="image/png",
                                  task_id=task_id, slot=slot, username=username,
                                  product_id="P-3001", source=source)


def _totals(username: str) -> dict:
    with session_scope() as session:
        return usage_store.totals(session, username=username)


# ---------------------------------------------------------------- P1-1：上架率不再 >100%
def test_listing_rate_stays_at_or_below_one(env):
    """复现 166.7% 场景：3 张使用量 + 5 张采纳（其中 2 张是复用图）。

    旧逻辑把复用图也算进 `adopted` → 5/3 = 166.7%；新逻辑复用图进 `reused_adopted`，
    上架率分子只算新生成图的采纳（3/3 = 100%）。用专属账号 + `?username=` 隔离计数。
    """
    headers = login(env)
    user = f"u-listing-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        usage_store.record(session, username=user, delivered=3, attempted=3,
                           tasks=1, agent_passed=3)
        reuse_ids = [_make_asset(session, kind="output", source="reuse",
                                 slot=10 + index, username=user).asset_id for index in range(2)]
        normal_ids = [_make_asset(session, kind="output", slot=20 + index,
                                  username=user).asset_id for index in range(3)]
        for asset_id in [*reuse_ids, *normal_ids]:
            feedback_store.record(session, task_id="t-listing", asset_id=asset_id,
                                  verdict="adopted", username=user)

    cards = _cards(env, headers, username=user)
    assert cards["usage_images"] == 3
    assert cards["reused_adopted"] == 2          # 复用图采纳单列，只做 hint
    assert cards["adopted"] == 3                 # 分子只算新生成图采纳
    assert cards["listing_rate"] <= 1.0          # 旧逻辑这里是 5/3 = 166.7%
    assert cards["listing_rate"] == pytest.approx(1.0, abs=1e-4)


def test_reused_adopted_is_kept_out_of_adopted_and_rejected(env):
    user = f"u-reuse-{uuid.uuid4().hex[:8]}"
    with session_scope() as session:
        usage_store.record(session, username=user, delivered=1, attempted=1, agent_passed=1)
        reuse_asset = _make_asset(session, kind="output", source="reuse", username=user).asset_id
        feedback_store.record(session, task_id="t-reuse", asset_id=reuse_asset,
                              verdict="adopted", username=user)
    totals = _totals(user)
    assert totals["reused_adopted"] == 1
    assert totals["adopted"] == 0 and totals["rejected"] == 0
    assert totals["images"] == 1                 # 采纳不改使用量/交付口径


# ---------------------------------------------------------------- P1-2：按终态记账
def test_verdict_flip_flop_counts_current_state_only(env):
    user = f"u-flip-{uuid.uuid4().hex[:8]}"
    path = ["adopted", "adopted", "rejected", "adopted"]
    with session_scope() as session:
        usage_store.record(session, username=user, delivered=1, attempted=1, agent_passed=1)
        asset = _make_asset(session, kind="output", username=user)
        for index, verdict in enumerate(path):
            assert (asset.adopt_status or "pending") in {"pending", "adopted", "rejected"}
            feedback_store.record(session, task_id="t-flip", asset_id=asset.asset_id,
                                  verdict=verdict, username=user,
                                  reason="改判" if verdict == "rejected" else "")
            totals = usage_store.totals(session, username=user)
            if verdict == "adopted":
                assert totals["adopted"] == 1, f"第 {index + 1} 步：采纳只能算 1 次"
            else:
                assert totals["adopted"] == 0, f"第 {index + 1} 步：切到驳回必须回退采纳"
            assert totals["rejected"] == (1 if verdict == "rejected" else 0)
    assert _totals(user)["adopted"] == 1


def test_verdict_flip_flop_via_api(env):
    headers = login(env)
    detail = _done_task(env, headers, product="P-3003")
    task_id = detail["task_id"]
    asset_id = detail["assets"][0]["asset_id"]
    path = f"/api/image/tasks/{task_id}/assets/{asset_id}"
    before = _cards(env, headers)

    for _ in range(2):                              # 重复点「采纳」不加计
        assert env.post(f"{path}/adopt", headers=headers).status_code == 200
    assert _cards(env, headers)["adopted"] - before["adopted"] == 1

    assert env.post(f"{path}/reject", headers=headers,
                    json={"reason": "改判"}).status_code == 200
    after_reject = _cards(env, headers)
    assert after_reject["adopted"] - before["adopted"] == 0     # 切到 rejected 回退 adopted

    assert env.post(f"{path}/adopt", headers=headers).status_code == 200
    after_adopt = _cards(env, headers)
    assert after_adopt["adopted"] - before["adopted"] == 1      # 回到 1，不是 2


# ---------------------------------------------------------------- P2-13：拦截图必须先放行
def test_blocked_asset_requires_unblock_before_adopt(env):
    headers = login(env)
    detail = _done_task(env, headers)
    task_id = detail["task_id"]
    with session_scope() as session:
        blocked = _make_asset(session, kind="blocked", task_id=task_id, slot=99).asset_id
    path = f"/api/image/tasks/{task_id}/assets/{blocked}"

    denied = env.post(f"{path}/adopt", headers=headers)
    assert denied.status_code == 422, denied.text
    assert denied.json()["error_code"] == "IMG_INPUT_INVALID"
    assert "unblock" in denied.json()["detail"]

    before = _cards(env, headers)
    assert env.post(f"{path}/unblock", headers=headers).status_code == 200
    after_unblock = _cards(env, headers)
    # 放行只补交付张数，不额外记使用量（第三方没有多生成）
    assert after_unblock["usage_images"] == before["usage_images"]
    # P1-4（第八轮）：放行也算交付 —— 使用量流水的 agent_passed 必须同步 +1，
    # 否则卡片「交付张数」(usage.agent_passed) 与任务台账 (images_delivered) 永久对不上。
    assert after_unblock["agent_passed"] - before["agent_passed"] == 1
    assert after_unblock["images_delivered"] - before["images_delivered"] == 1

    assert env.post(f"{path}/adopt", headers=headers).status_code == 200
    assert _cards(env, headers)["adopted"] - before["adopted"] == 1


def test_reject_on_blocked_asset_is_also_denied(env):
    headers = login(env)
    detail = _done_task(env, headers)
    task_id = detail["task_id"]
    with session_scope() as session:
        blocked = _make_asset(session, kind="blocked", task_id=task_id, slot=98).asset_id
    resp = env.post(f"/api/image/tasks/{task_id}/assets/{blocked}/reject",
                    headers=headers, json={"reason": "被拦的图不该进负样本"})
    assert resp.status_code == 422


def test_adopt_by_admin_is_attributed_to_asset_owner(env):
    """P2-5（第八轮）：管理员代运营采纳时，使用量要记在图片归属人名下。"""
    from app.agents_image.store import tasks as task_store

    headers = login(env)
    owner = f"u-owner-{uuid.uuid4().hex[:8]}"
    task_id = "t-attr" + uuid.uuid4().hex[:8]
    with session_scope() as session:
        usage_store.record(session, username=owner, delivered=1, attempted=1,
                           tasks=1, agent_passed=1)
        asset = _make_asset(session, kind="output", username=owner, task_id=task_id)
        task_store.create_task(session, task_id=task_id, username=owner, department="女装",
                               prompt="白底主图", platform="淘系", aspect="1:1", n=1,
                               ref_asset_ids=[], product_id="P-3001")
        asset_id = asset.asset_id

    before_owner = _totals(owner)
    before_admin = _totals("admin")
    assert env.post(f"/api/image/tasks/{task_id}/assets/{asset_id}/adopt",
                    headers=headers).status_code == 200
    after_owner = _totals(owner)
    after_admin = _totals("admin")
    assert after_owner["adopted"] - before_owner["adopted"] == 1, "应记在图片归属人名下"
    assert after_admin["adopted"] == before_admin["adopted"], "不能记到管理员（操作人）头上"


def test_non_output_asset_cannot_be_adopted(env):
    headers = login(env)
    detail = _done_task(env, headers)
    task_id = detail["task_id"]
    with session_scope() as session:
        library = _make_asset(session, kind="library", task_id=task_id, slot=0).asset_id
    resp = env.post(f"/api/image/tasks/{task_id}/assets/{library}/adopt", headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "IMG_INPUT_INVALID"
