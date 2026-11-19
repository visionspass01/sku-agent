"""第四轮 W1：选色参数契约（P0-1）+ SSE 快照任务参数（P1-10）。

三层验证：
1. 纯函数层：直接跑 `web/image_payload.js`（Node require），验混合/全选/无色/不勾四种场景；
2. 源码层：image.js 必须走纯函数、旧 `allColors` 逻辑必须消失、页面脚本顺序正确；
3. HTTP 层：按纯函数产出的参数真建任务，断言混合选择既不多买颜色也不丢色。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agents_image import metrics as image_metrics
from app.agents_image.auth import reset_throttle
from app.agents_image.config import reset_image_settings
from app.agents_image.sources import batch_context
from app.agents_image.sources import product_source as ps
from app.agents_image.store import users as user_store
from app.agents_image.store.schema import ensure_image_schema
from app.db.engine import session_scope

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
NODE = shutil.which("node") or shutil.which("node.exe")

# 纯函数的四个场景（商品清单形状与 /api/image/products 返回一致）
_PAYLOAD_CASES = r"""
const payload = require(process.env.IMAGE_PAYLOAD_PATH);
const products = [
  { product_id: "A", colors: [{ color: "米色" }, { color: "黑色" }, { color: "藏蓝" }] },
  { product_id: "B", colors: [{ color: "红色" }, { color: "白色" }] },
  { product_id: "C", colors: [] },
  { product_id: "D", colors: [{ color: "" }] },
];
function plan(selected, chosen) {
  return payload.buildTaskPayload({
    selected,
    products,
    colorsMap: new Map(Object.entries(chosen || {})),
  });
}
const out = {
  mixed: plan(["A", "B"], { A: ["米色", "黑色", "藏蓝"], B: ["红色"] }),
  full: plan(["A", "B"], { A: ["米色", "黑色", "藏蓝"], B: ["红色", "白色"] }),
  plain: plan(["A", "C", "D"], { A: ["米色"] }),
  untouched: plan(["A", "B"], { A: ["米色", "黑色", "藏蓝"] }),
  only_default: plan(["D"], {}),
  labels: [payload.colorLabel(""), payload.colorLabel("米色")],
};
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def payload_plan(tmp_path_factory) -> dict:
    """在 Node 里真跑一遍 buildTaskPayload（跳过条件：机器上没有 node）。"""
    if NODE is None:
        pytest.skip("需要 node 才能执行前端纯函数")
    script = tmp_path_factory.mktemp("payload") / "plan.js"
    script.write_text(_PAYLOAD_CASES, encoding="utf-8")
    env = dict(os.environ)
    env["IMAGE_PAYLOAD_PATH"] = str(WEB / "image_payload.js")
    env.setdefault("ELECTRON_RUN_AS_NODE", "1")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True,
                          encoding="utf-8", env=env, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _colors_of(result: dict, product_id: str) -> list[str] | None:
    for entry in result["product_colors"]:
        if entry["product_id"] == product_id:
            return entry["colors"]
    return None


# ---------------------------------------------------------------- 纯函数契约
def test_mixed_selection_keeps_full_product_and_partial_product(payload_plan):
    mixed = payload_plan["mixed"]
    assert mixed["all_colors"] is False
    # 勾满的商品必须显式列出全部颜色（旧逻辑会把它整体丢弃 → 丢色）
    assert _colors_of(mixed, "A") == ["米色", "黑色", "藏蓝"]
    # 只勾部分的商品只出已选颜色
    assert _colors_of(mixed, "B") == ["红色"]


def test_all_colors_only_when_every_product_fully_selected(payload_plan):
    full = payload_plan["full"]
    assert full["all_colors"] is True
    assert full["product_colors"] == []          # 全选时交给中台统一展开，不重复发
    assert full["color_count"] == 5


def test_unselected_product_falls_back_to_first_color(payload_plan):
    untouched = payload_plan["untouched"]
    assert untouched["all_colors"] is False      # 旧逻辑这里仍为 True → 多买颜色（资损）
    assert _colors_of(untouched, "A") == ["米色", "黑色", "藏蓝"]
    assert _colors_of(untouched, "B") == ["红色"]


def test_products_without_color_dimension_are_absent(payload_plan):
    plain = payload_plan["plain"]
    assert _colors_of(plain, "C") is None        # colors: []
    assert _colors_of(plain, "D") is None        # colors: [{color: ""}]
    assert plain["all_colors"] is False
    assert _colors_of(plain, "A") == ["米色"]


def test_default_color_label_never_enters_payload(payload_plan):
    assert payload_plan["labels"] == ["默认", "米色"]
    only_default = payload_plan["only_default"]
    assert only_default["product_colors"] == []
    assert only_default["all_colors"] is False


# ---------------------------------------------------------------- SSE 快照（P1-10）
def _task_stub(**overrides):
    base = dict(
        task_id="t-1", batch_id="b-1", product_id="P-1001", platform="淘系",
        variant_id="A", color_key="P-1001#米色", base_task_id=None, status="running",
        attempts=1, slot_count=9, image_count=6, ref_source="mcp", retries=2,
        provider_calls=7, attempted_images=6, delivered_images=2,
        warnings_json=["IMG_X"], blocked_json=[2], backfill_status="pending",
        error_code="", slots_json=[{"slot": 1}],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_snapshot_carries_task_parameters():
    from app.agents_image.api.stream_routes import _snapshot_payload

    snapshot = _snapshot_payload(_task_stub(status="done"))
    assert snapshot["slot_count"] == 9
    assert snapshot["image_count"] == 6
    assert snapshot["ref_source"] == "mcp"
    assert snapshot["retries"] == 2
    assert snapshot["provider_calls"] == 7
    # 原有字段不能被顺手改掉
    assert snapshot["expected_images"] == 6
    assert snapshot["partial"] is True
    assert snapshot["in_progress"] is False

    # 第四轮 P1-9：运行中的任务不算"部分成功"
    running = _snapshot_payload(_task_stub(status="running"))
    assert running["partial"] is False and running["message"] == ""
    assert running["in_progress"] is True


# ---------------------------------------------------------------- 源码契约
def test_image_js_delegates_to_payload_module():
    js = (WEB / "image.js").read_text(encoding="utf-8")
    assert "buildColorPlan" in js and "ImagePayload" in js
    assert "let allColors" not in js            # 旧的全有或全无逻辑必须消失
    # iterate 的图位数取快照字段，不回落读 UI 输入框
    assert "data.slot_count || data.image_count" in js


def test_image_page_script_order_and_no_empty_scope_note():
    html = (WEB / "image.html").read_text(encoding="utf-8")
    assert html.index("/image_payload.js") < html.index("/image.js")
    assert 'id="scope-note"' not in html


# ---------------------------------------------------------------- HTTP 层（真建任务）
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


def _login(client, username="admin", password="hhw123") -> dict:
    resp = client.post("/api/image/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _wait(client, headers, task_id, timeout=25.0) -> dict:
    deadline = time.time() + timeout
    detail: dict = {}
    while time.time() < deadline:
        detail = client.get(f"/api/image/tasks/{task_id}", headers=headers).json()
        if detail.get("status") in {"done", "failed", "canceled"}:
            return detail
        time.sleep(0.05)
    return detail


def test_mixed_color_selection_creates_exact_targets(env):
    """P-1001 只勾 1 个颜色 + P-3002 勾满 2 个 → 正好 3 个任务，一个不多一个不少。"""
    headers = _login(env)
    resp = env.post("/api/image/tasks", headers=headers, json={
        "product_ids": ["P-1001", "P-3002"],
        "product_colors": [{"product_id": "P-1001", "colors": ["米色"]},
                           {"product_id": "P-3002", "colors": ["正红", "黑色"]}],
        "prompt": "白底主图", "slot_count": 3, "image_count": 1,
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    pairs = sorted((item["product_id"], item["color"]) for item in body["tasks"])
    assert pairs == [("P-1001", "米色"), ("P-3002", "正红"), ("P-3002", "黑色")]
    for item in body["tasks"]:
        assert _wait(env, headers, item["task_id"])["status"] == "done"
