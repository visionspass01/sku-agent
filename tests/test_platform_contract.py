"""中台契约测试（评审 2.1-4）。

三层保障，缺一层就会出现「字段悄悄改了、Agent 在用户答案里报 KeyError」：
1. **结构层**：每个 `/api/v1` 路由必须声明 `response_model`，且模型是 `Envelope` 子类
   （自动带 `schema_version`）——漏声明即失败，新增端点不可能绕过；
2. **运行时层**：真实响应的 body 带 `schema_version`、响应头带 `X-Platform-Contract`；
3. **调用方层**：Agent 侧契约校验对「主版本不兼容」返回 `error_kind="contract"` 且不触发熔断；
   缺少版本号默认按 legacy 1.0 兼容，严格模式（生产）下才报错。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

TOKEN = {"X-Internal-Token": "demo-internal-token"}


@pytest.fixture(scope="module")
def platform_app(tmp_path_factory):
    from data_platform.api import cache as pcache
    from data_platform.db import init_schema, make_engine, set_engine

    tmp = tmp_path_factory.mktemp("platform_contract")
    engine = make_engine(f"sqlite:///{(tmp / 'platform.db').as_posix()}")
    set_engine(engine)
    init_schema(engine)
    pcache.reset_client()

    from data_platform.api.main import app

    yield app
    set_engine(None)


@pytest.fixture()
def client(platform_app):
    with TestClient(platform_app) as c:
        yield c


# ---------------------------------------------------------------- 1. 结构层
def test_every_api_route_declares_contract_model(platform_app):
    from data_platform.api.schemas import Envelope

    missing: list[str] = []
    not_envelope: list[str] = []
    for route in platform_app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/v1/") or not hasattr(route, "response_model"):
            continue
        model = getattr(route, "response_model", None)
        if model is None:
            missing.append(f"{sorted(getattr(route, 'methods', []) or [])} {path}")
            continue
        if not (isinstance(model, type) and issubclass(model, Envelope)):
            not_envelope.append(f"{path} -> {model}")
    assert not missing, f"这些路由没有声明 response_model：{missing}"
    assert not not_envelope, f"这些路由的 response_model 不是 Envelope 子类：{not_envelope}"


def test_envelope_defaults_to_current_contract_version():
    from data_platform.api import schemas as sc
    from data_platform.contract import CONTRACT_VERSION

    assert sc.Envelope().schema_version == CONTRACT_VERSION
    # 抽查几个业务模型：默认值必须继承（否则路由就得手写版本号，早晚漏）
    for model in (sc.SkuMetricsOut, sc.ApprovalsOut, sc.HealthOut):
        assert model.model_fields["schema_version"].default == CONTRACT_VERSION


# ---------------------------------------------------------------- 2. 运行时层
def test_response_body_and_header_carry_version(client):
    from data_platform.contract import CONTRACT_HEADER, CONTRACT_VERSION

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.headers[CONTRACT_HEADER] == CONTRACT_VERSION
    assert health.json()["schema_version"] == CONTRACT_VERSION

    deps = client.get("/api/v1/metadata/departments", headers=TOKEN)
    assert deps.status_code == 200
    assert deps.json()["schema_version"] == CONTRACT_VERSION
    # 部门映射带内容指纹：Agent 侧据此判断要不要刷新本地缓存（改映射不需重启）
    assert deps.json()["version"], "部门映射缺少版本指纹"


def test_metadata_metrics_exposes_contract(client):
    body = client.get("/api/v1/metadata/metrics", headers=TOKEN).json()
    assert body["contract"]["contract_version"]
    assert body["contract"]["supported_client_versions"]


def test_data_version_header_on_read_endpoints(client):
    from data_platform.contract import DATA_VERSION_HEADER

    resp = client.get("/api/v1/skus/ST-01-C1-M/metrics", headers=TOKEN)
    if resp.status_code != 200:      # 该测试库未初始化 mart 时跳过（由 test_platform.py 覆盖）
        pytest.skip(f"mart 未就绪：{resp.status_code}")
    assert resp.headers[DATA_VERSION_HEADER].isdigit()
    assert resp.json()["data_version"] == int(resp.headers[DATA_VERSION_HEADER])


# ---------------------------------------------------------------- 3. 调用方层
def test_contract_check_semantics():
    from app.tools import platform_contract as contract

    assert contract.check({"schema_version": "1.1"}, "") is None
    assert contract.check({"schema_version": "1.9"}, "") is None       # 次版本兼容
    assert contract.check({"schema_version": "1.0"}, "") is None       # legacy
    assert contract.check({}, "") is None                              # 缺版本 = legacy（宽松）
    assert contract.check({}, "", strict=True)["error_kind"] == "contract"
    err = contract.check({"schema_version": "2.0"}, "")
    assert err and err["error_kind"] == "contract" and err["next_step"]
    # 头部优先于 body
    assert contract.check({"schema_version": "1.1"}, "2.0")["error_kind"] == "contract"


def test_platform_client_surfaces_incompatible_contract():
    from app.tools import platform_contract as contract
    from app.tools import platform_tools

    contract.reset_stats()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"sku_id": "ST-01-C1-M", "available": 10, "schema_version": "2.0"},
            headers={"X-Platform-Contract": "2.0", "X-Request-Id": "rid-1"},
        )

    client = platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler),
    )
    platform_tools.set_platform_client(client)
    try:
        import asyncio

        payload = asyncio.run(client.get("/api/v1/skus/ST-01-C1-M/metrics"))
        assert payload["error_kind"] == "contract"
        assert payload["remote_contract"] == "2.0"
        # 契约不匹配不算链路故障：熔断器必须保持关闭，否则会连带熔断正常接口
        assert client.breaker_state()["state"] == "closed"
        assert contract.stats()["mismatch"] == 1
    finally:
        platform_tools.set_platform_client(None)


def test_platform_client_accepts_versioned_payload():
    from app.tools import platform_tools

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"sku_id": "ST-01-C1-M", "available": 10, "schema_version": "1.1"},
            headers={"X-Platform-Contract": "1.1"},
        )

    client = platform_tools.PlatformClient(
        base_url="http://mock.platform", token="t", transport=httpx.MockTransport(handler),
    )
    platform_tools.set_platform_client(client)
    try:
        import asyncio

        payload = asyncio.run(client.get("/api/v1/skus/ST-01-C1-M/metrics"))
        assert payload["available"] == 10
        assert "error" not in payload
    finally:
        platform_tools.set_platform_client(None)
