"""追问接口越权防线：线程的 state.sku_id 必须与请求的 sku_id 一致。

背景（真实漏洞，先红后绿）：
`POST /api/inspect/followup` 早期只校验「请求里的 sku_id 属于本部门」，
但真正读的数据来自请求里的 `thread_id`。于是男装账号可以传一个女装线程的 id
（`sku:ST-90-C1-M`）而把 sku_id 填成本部门 SKU 混过校验，拿到女装 SKU 的采集数据与结论。

修复方式：以 state 里的 `sku_id` 为准做二次校验，不一致直接 403（ThreadAccessError）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest


class _FakeGraph:
    def __init__(self, state: dict) -> None:
        self._state = state
        self.streamed = False

    async def aget_state(self, config):  # noqa: ANN001
        return SimpleNamespace(values=self._state)

    async def astream(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.streamed = True
        yield {"reasoner": {"status": "ok"}}


def _patch_graph(monkeypatch, state: dict):
    from app.pipeline import patrol

    graph = _FakeGraph(state)

    @asynccontextmanager
    async def _fake_graph():
        yield graph

    monkeypatch.setattr(patrol, "inspection_graph", _fake_graph)
    return graph


WOMEN_STATE = {"sku_id": "ST-90-C1-M", "status": "ok", "plan": {"risk_level": "高"}}
MEN_STATE = {"sku_id": "ST-92-C1-M", "status": "ok", "plan": {"risk_level": "中"}}


def test_thread_from_other_department_is_rejected(app_client, login, monkeypatch):
    """男装账号 + 女装线程 → 403（修复前的行为是 200 + 女装结论）。"""
    headers = login("worker1", "hhw111")
    graph = _patch_graph(monkeypatch, WOMEN_STATE)

    resp = app_client.post(
        "/api/inspect/followup", headers=headers,
        json={"sku_id": "ST-92-C1-M", "instruction": "这单要补多少？",
              "thread_id": "sku:ST-90-C1-M"},
    )
    assert resp.status_code == 403, resp.text
    assert "不一致" in resp.json()["detail"]
    # 被拒绝时不应该再跑 refine（既省 token，也避免把别人 state 写坏）
    assert graph.streamed is False


def test_thread_sku_mismatch_same_department_is_rejected(app_client, login, monkeypatch):
    """管理员也一样：线程与 SKU 不一致就 403，不做隐式放行。"""
    headers = login("admin", "hhw123")
    _patch_graph(monkeypatch, WOMEN_STATE)

    resp = app_client.post(
        "/api/inspect/followup", headers=headers,
        json={"sku_id": "ST-92-C1-M", "instruction": "继续", "thread_id": "sku:ST-90-C1-M"},
    )
    assert resp.status_code == 403


def test_matching_thread_still_works(app_client, login, monkeypatch):
    """正常路径不受影响：同一 SKU 的线程可以追问。"""
    headers = login("worker1", "hhw111")
    graph = _patch_graph(monkeypatch, MEN_STATE)

    resp = app_client.post(
        "/api/inspect/followup", headers=headers,
        json={"sku_id": "ST-92-C1-M", "instruction": "那优先调拨还是补货？",
              "thread_id": "sku:ST-92-C1-M"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sku_id"] == "ST-92-C1-M"
    assert graph.streamed is True


def test_missing_thread_returns_404(app_client, login, monkeypatch):
    headers = login("worker1", "hhw111")
    _patch_graph(monkeypatch, {})
    resp = app_client.post(
        "/api/inspect/followup", headers=headers,
        json={"sku_id": "ST-92-C1-M", "instruction": "继续"},
    )
    assert resp.status_code == 404
    assert "请先" in resp.json()["detail"]


def test_followup_requires_login(app_client):
    resp = app_client.post(
        "/api/inspect/followup",
        json={"sku_id": "ST-92-C1-M", "instruction": "继续"},
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("sku_id", ["ST-90-C1-M"])
def test_cross_department_sku_still_404(app_client, login, monkeypatch, sku_id):
    """直接问女装 SKU：仍按「看不到就不存在」返回 404（不泄露存在性）。"""
    headers = login("worker1", "hhw111")
    _patch_graph(monkeypatch, WOMEN_STATE)
    resp = app_client.post(
        "/api/inspect/followup", headers=headers,
        json={"sku_id": sku_id, "instruction": "继续"},
    )
    assert resp.status_code == 404
