"""中台 image 模块的测试支撑：临时库 + 应用客户端 + 进程内 ASGI 传输。"""

from __future__ import annotations

TOKEN = {"X-Internal-Token": "demo-internal-token"}


def init_platform_db(tmp_path):
    """把中台指向临时 sqlite 并建表 + 播种 image_catalog。"""
    from data_platform import image_mock
    from data_platform.db import init_schema, make_engine, session_scope, set_engine

    engine = make_engine(f"sqlite:///{(tmp_path / 'platform.db').as_posix()}")
    set_engine(engine)
    init_schema(engine)
    with session_scope() as session:
        image_mock.seed_catalog_rows(session)
    return engine


def platform_client():
    from fastapi.testclient import TestClient

    from data_platform.api.main import app

    return TestClient(app)


def platform_transport():
    """让 Agent 侧 MCP 工具在进程内直连中台应用（离线、无端口）。"""
    import httpx

    from data_platform.api.main import app

    return httpx.ASGITransport(app=app)


def reset_platform_engine() -> None:
    from data_platform.db import set_engine

    set_engine(None)
