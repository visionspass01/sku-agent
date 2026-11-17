"""中台 image 模块契约：商品清单 / 上下文（含参考图）/ 预设 / 图位策略 / 规则 / 结果回写幂等。"""

from __future__ import annotations

import pytest

from tests.image_platform_support import (
    TOKEN,
    init_platform_db,
    platform_client,
    reset_platform_engine,
)

# 结果回写需要 write scope：默认令牌是全权主体，够用
HEADERS = TOKEN


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("platform_image")
    init_platform_db(tmp)
    with platform_client() as c:
        yield c
    reset_platform_engine()


def test_image_endpoints_require_token(client):
    for path in ("/api/v1/image/products", "/api/v1/image/products/P-1001/context",
                 "/api/v1/image/presets/compositions", "/api/v1/image/plan-policy",
                 "/api/v1/image/platform-rules/淘系", "/api/v1/image/results",
                 "/api/v1/image/assets/P-1001-img-1.png"):
        assert client.get(path).status_code == 401, path


def test_products_list_and_filter(client):
    body = client.get("/api/v1/image/products", headers=HEADERS).json()
    assert body["count"] >= 5
    ids = [item["product_id"] for item in body["products"]]
    assert "P-1001" in ids and "P-1002" in ids
    filtered = client.get("/api/v1/image/products", params={"keyword": "工装"},
                          headers=HEADERS).json()
    assert [item["product_id"] for item in filtered["products"]] == ["P-1002"]
    limited = client.get("/api/v1/image/products", params={"limit": 2}, headers=HEADERS).json()
    assert limited["count"] == 2


def test_product_context_has_lock_fields_and_images(client):
    body = client.get("/api/v1/image/products/P-1001/context", headers=HEADERS).json()
    assert body["category"] == "女装"
    assert body["attrs"]["must_not_change"]
    assert body["attrs"]["color_hex"].startswith("#")
    assert body["sku_ids"]
    assert len(body["images"]) == 3
    first = body["images"][0]
    assert first["is_main"] is True
    assert first["url"] == "/api/v1/image/assets/P-1001-img-1.png"


def test_unknown_product_is_404(client):
    resp = client.get("/api/v1/image/products/P-9999/context", headers=HEADERS)
    assert resp.status_code == 404


def test_image_asset_returns_png(client):
    resp = client.get("/api/v1/image/assets/P-1001-img-1.png", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content.startswith(b"\x89PNG")
    # 同 id 同字节（确定性，便于断言）
    again = client.get("/api/v1/image/assets/P-1001-img-1.png", headers=HEADERS)
    assert again.content == resp.content


def test_broken_image_is_404(client):
    assert client.get("/api/v1/image/assets/P-4002-img-1.png",
                      headers=HEADERS).status_code == 404


def test_presets_come_from_catalog_table(client):
    compositions = client.get("/api/v1/image/presets/compositions", headers=HEADERS).json()
    labels = [item["label"] for item in compositions["items"]]
    assert "正面全身像·上装突出" in labels and "侧身像·下装突出" in labels
    styles = client.get("/api/v1/image/presets/scene-styles", headers=HEADERS).json()
    style_labels = [item["label"] for item in styles["items"]]
    assert {"影棚正式风", "街拍清新风", "工厂实用风"} <= set(style_labels)
    assert all(item["kind"] == "scene_style" for item in styles["items"])


def test_plan_policy_gives_nine_slots_and_defaults(client):
    body = client.get("/api/v1/image/plan-policy", headers=HEADERS).json()
    assert body["default_count"] == 6 and body["max_count"] == 9
    assert body["default_composition_id"] == "C-FULL-FRONT-TOP"
    assert [item["slot"] for item in body["slots"]] == list(range(1, 10))
    assert body["slots"][6]["type"] == "layout"        # 尺码对照不走出图通道
    assert body["slots"][8]["type"] == "photo_required"


def test_platform_rules_with_fallback(client):
    taoxi = client.get("/api/v1/image/platform-rules/淘系", headers=HEADERS).json()
    assert taoxi["aspect"] == "1:1" and "全网最低价" in taoxi["forbidden_words"]
    unknown = client.get("/api/v1/image/platform-rules/未知平台", headers=HEADERS).json()
    assert unknown["platform"] == "通用"


def test_results_backfill_is_idempotent(client):
    payload = {
        "request_id": "req-int-1", "batch_id": "b-1", "product_id": "P-1001",
        "task_id": "20261117153045a3f9", "platform": "淘系", "aspect": "1:1", "count": 2,
        "compositions": ["C-FULL-FRONT-TOP"], "scene_styles": ["S-WHITE-E-COM"],
        "prompt_final": "白底主图", "prompt_pack": [{"slot": 1, "prompt": "p1"}],
        "params": {"provider": "fake"}, "requester": "worker1", "trace_id": "t-1",
        "results": [
            {"slot": 1, "role": "主图", "prompt": "p1", "sha256": "a" * 64,
             "mime": "image/png", "width": 640, "height": 640, "bytes": 100,
             "storage": "local", "storage_key": "outputs/a.png"},
            {"slot": 2, "role": "上装卖点", "prompt": "p2", "sha256": "b" * 64,
             "mime": "image/png", "width": 640, "height": 640, "bytes": 120,
             "storage": "local", "storage_key": "outputs/b.png"},
        ],
    }
    first = client.post("/api/v1/image/results", json=payload, headers=HEADERS).json()
    assert first["saved"] == 2 and first["deduped"] == 0

    again = client.post("/api/v1/image/results", json=payload, headers=HEADERS).json()
    assert again["saved"] == 0 and again["deduped"] == 2      # (task_id, slot) 唯一

    rows = client.get("/api/v1/image/results", params={"product_id": "P-1001"},
                      headers=HEADERS).json()
    assert rows["count"] == 2
    assert {item["slot"] for item in rows["items"]} == {1, 2}
    assert all(item["sha256"] for item in rows["items"])


def test_request_snapshot_written_once(client):
    from sqlalchemy import select

    from data_platform.db import ImageRequest, session_scope

    with session_scope() as session:
        rows = session.execute(select(ImageRequest)).scalars().all()
        snapshot = [(row.request_id, row.batch_id) for row in rows]
    assert [item[0] for item in snapshot].count("req-int-1") == 1
    assert ("req-int-1", "b-1") in snapshot
