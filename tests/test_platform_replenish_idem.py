"""补货单的幂等兜底回归：无幂等键时，唯一约束冲突**不能**返回别人的审批单。

背景（真实 CI 事故，`verify@main` 上间歇性红）：

- `PO{分钟}{uuid4().hex[:4]}` 是 `erp_replenish_order` 的主键，同一分钟内撞号就会抛
  `IntegrityError`；
- 旧兜底 `select(ApprovalRequest).where(idempotency_key == idem_key)` 在 `idem_key is None`
  时等价于 `WHERE idempotency_key IS NULL`，会命中**历史上任何一张**没有幂等键的审批单；
- 于是接口回 `deduped:true` 并给出别人的 `approval_id`，调用方审批它就会跨部门通过
  （`tests/test_platform_authz.py::test_approve_is_scoped_to_department` 就是这么红的）。

这里固定 `uuid4` 制造主键冲突，断言两件事：
1. 无幂等键 + 主键冲突 → 报错（500），且**不**返回别人的审批单；
2. 有幂等键 + 重发 → 仍然按幂等命中返回同一张单（正常路径没被误伤）。
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_mod

import pytest
from fastapi.testclient import TestClient

TOKEN = {"X-Internal-Token": "demo-internal-token"}
WOMEN_SKU = "ST-01-C1-M"
MEN_SKU = "ST-05-C1-M"


@pytest.fixture(scope="module")
def platform_ready(tmp_path_factory):
    from data_platform.api import cache as pcache
    from data_platform.db import init_schema, make_engine, set_engine
    from data_platform.ingest.airflow_mock import run_dag
    from data_platform.mock_sources import generate_all

    tmp = tmp_path_factory.mktemp("platform_replenish_idem")
    engine = make_engine(f"sqlite:///{(tmp / 'platform.db').as_posix()}")
    set_engine(engine)
    init_schema(engine)
    pcache.reset_client()
    generate_all(snapshot_date=dt.date(2026, 11, 3), seed=7)
    result = run_dag("daily_sync")
    assert all(t["status"] == "success" for t in result["tasks"].values()), result
    yield engine
    set_engine(None)


@pytest.fixture()
def client(platform_ready):
    from data_platform.api.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class _FixedUuid:
    """把 `uuid.uuid4()` 固定住 → 同一分钟内两次下单生成同一个 po_no（主键冲突）。"""

    hex = "c0ffee00c0ffee00c0ffee00c0ffee00"

    def __call__(self):  # noqa: D401 —— 让 `uuid.uuid4()` 调用本身也返回固定值
        return uuid_mod.UUID(hex=self.hex)


def _patch_uuid(monkeypatch) -> None:
    from data_platform.api import routes as platform_routes

    fixed = _FixedUuid()
    monkeypatch.setattr(platform_routes.uuid, "uuid4", lambda: uuid_mod.UUID(hex=fixed.hex),
                        raising=True)


def test_po_no_collision_must_not_return_foreign_approval(client, monkeypatch):
    """无幂等键 + 主键冲突：不能把上一张单当成"幂等命中"塞回来。"""
    from data_platform.db import ApprovalRequest, session_scope

    _patch_uuid(monkeypatch)

    first = client.post("/api/v1/erp/replenish-orders", headers=TOKEN,
                        json={"sku_id": MEN_SKU, "qty": 500, "reason": "男装待批"})
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["deduped"] is False

    second = client.post("/api/v1/erp/replenish-orders", headers=TOKEN,
                         json={"sku_id": WOMEN_SKU, "qty": 500, "reason": "女装待批"})
    # 主键冲突 → 请求失败；关键是不能"成功"返回别人的单
    assert second.status_code >= 500, second.text
    assert "approval_id" not in second.text
    assert first_body["approval_id"] not in second.text

    with session_scope() as session:
        # 出会话后 ORM 实例会 detach，属性只能在会话内读
        skus = [r.sku_id for r in session.query(ApprovalRequest).all()]
    assert skus == [MEN_SKU], "冲突请求不应写入半成品审批单"


def test_same_idempotency_key_still_dedupes(client):
    """正常路径没被误伤：带幂等键重发仍然命中同一张单。"""
    payload = {"sku_id": WOMEN_SKU, "qty": 500, "reason": "幂等重发",
               "idempotency_key": "idem-replenish-1"}
    first = client.post("/api/v1/erp/replenish-orders", headers=TOKEN, json=payload).json()
    second = client.post("/api/v1/erp/replenish-orders", headers=TOKEN, json=payload).json()

    assert first["deduped"] is False
    assert second["deduped"] is True
    assert second["approval_id"] == first["approval_id"]
    assert second["sku_id"] == WOMEN_SKU
