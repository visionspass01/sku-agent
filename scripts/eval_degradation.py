"""降级路径测评：依赖坏掉时系统**表现如何**（离线，可作 CI 门槛）。

为什么单独一个脚本：`tests/` 验的是"正常路径对不对"，而真实事故几乎都发生在依赖异常时——
中台挂了、契约升级了、向量库连不上、重排超时、Redis 抖动。这些路径如果没被验证过，
线上表现往往就是"每个工具先等满 15 秒超时"或者"整轮对话 500"。

六个场景（每个都断言"可诊断 + 快速失败 + 不静默降质"）：
1. 中台连不上        → 工具返回 transport 错误 + 熔断打开（后续快速失败）
2. 中台契约不兼容    → error_kind=contract，**不**计入熔断（中台是健康的）
3. 向量库不可用      → 检索自动降级为本地哈希余弦，仍有结果
4. 重排超时          → 回退融合序，且 rerank_degraded=True（诚实标注）
5. LLM 超时/失败     → 工具/对话给出可读错误，不编造数据
6. Redis 不可用      → 会话锁 fail-closed（503）或按配置降级并标记

用法：
    python scripts/eval_degradation.py --min-pass 1.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def _setup(tmp: pathlib.Path):  # noqa: ANN201
    from app.config import Settings, set_settings
    from app.db.engine import init_schema, make_engine, set_engine

    settings = Settings(
        database_url=f"sqlite:///{(tmp / 'agent.db').as_posix()}",
        redis_url="",
        mcp_transport="direct",
        scheduler_enabled=False,
        analysis_merge_mode="keep-both",
        grounding_enabled=False,
        department_mapping_cache=False,
        catalog_sync_on_startup=False,
        platform_api_url="http://127.0.0.1:8001",
        platform_timeout_seconds=2.0,
        platform_breaker_threshold=2,
        platform_breaker_reset_seconds=30.0,
        rag_rerank_backend="llm",
        rag_rerank_timeout_ms=50,
        deepseek_api_key="sk-degradation-test",
    )
    settings.analyses_dir = str(tmp / "analyses")
    set_settings(settings)
    engine = make_engine(settings)
    set_engine(engine)
    init_schema(engine)

    # 种子：一个 SKU + 一份知识库，保证检索类场景有东西可查
    from app.db.engine import session_scope
    from app.db.models import Sku
    from app.rag.index import build_index

    with session_scope() as session:
        session.add(Sku(
            sku_id="ST-01-C1-M", style_id="ST-01", style_name="测试款", category="女装",
            season="秋冬", color="黑色", size="M", warehouse_id="WH-HZ", platform="淘系",
            price=299, list_price=299, stock=100, return_rate=0.05, supplier_id="SUP-01",
            status="在售",
        ))
    with session_scope() as session:
        build_index(session)
    return settings


def _cases(settings) -> list[dict]:  # noqa: ANN001
    import httpx

    from app.auth import AuthUser
    from app.tools import platform_tools
    from app.tools.platform_tools import build_platform_tools

    admin = AuthUser(username="admin", role="admin", department=None)
    results: list[dict] = []

    def case(name: str, ok: bool, detail: str = "") -> None:
        results.append({"name": name, "ok": bool(ok), "detail": str(detail)[:240]})

    # ① 中台连不上：transport 错误 + 熔断打开
    def _down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = platform_tools.PlatformClient(base_url="http://down.local", token="t",
                                          transport=httpx.MockTransport(_down))
    client._breaker = __import__("app.resilience", fromlist=["Breaker"]).Breaker(
        threshold=2, reset_seconds=30.0)
    platform_tools.set_platform_client(client)
    tools = {t.name: t for t in build_platform_tools(admin)}
    first = asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-01-C1-M"}))
    second = asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-01-C1-M"}))
    case("中台连不上：返回 transport 错误且不给假数据",
         "error" in first and first.get("error_kind") == "transport", first.get("error_kind"))
    # 第二次换一个 SKU（不同缓存 key）：要么负缓存快速失败、要么熔断已打开，两种都算"不再等满超时"
    third = asyncio.run(tools["get_inventory"].ainvoke({"sku_id": "ST-02-C1-M"}))
    case("中台连不上：后续调用快速失败（负缓存或熔断，不再等满超时）",
         bool(second.get("negative_cached") or second.get("breaker_open")
              or third.get("negative_cached") or third.get("breaker_open")
              or client.breaker_state()["state"] == "open"),
         {"second": second.get("error_kind"), "third": third.get("error_kind"),
          "breaker": client.breaker_state()})

    # ② 契约不兼容：contract 错误，不计熔断
    def _future(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sku_id": "ST-01-C1-M", "available": 1,
                                         "schema_version": "2.0"},
                              headers={"X-Platform-Contract": "2.0"})

    contract_client = platform_tools.PlatformClient(
        base_url="http://future.local", token="t", transport=httpx.MockTransport(_future))
    platform_tools.set_platform_client(contract_client)
    tools2 = {t.name: t for t in build_platform_tools(admin)}
    payload = asyncio.run(tools2["get_inventory"].ainvoke({"sku_id": "ST-01-C1-M"}))
    case("契约不兼容：error_kind=contract 且提示联系管理员",
         payload.get("error_kind") == "contract" and "管理员" in str(payload.get("next_step")),
         payload.get("error_kind"))
    case("契约不兼容：不把中台判成不健康（熔断保持关闭）",
         contract_client.breaker_state()["state"] == "closed",
         contract_client.breaker_state())

    # ③ 向量库不可用：降级内存检索仍然有结果
    def _ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sku_id": "ST-01-C1-M", "available": 1,
                                         "schema_version": "1.1"})

    good = platform_tools.PlatformClient(base_url="http://ok.local", token="t",
                                        transport=httpx.MockTransport(_ok))
    platform_tools.set_platform_client(good)
    from app.rag import vector_store as vs
    from app.rag.hybrid import get_retriever, invalidate_retriever

    original_build = vs.build_store
    vs.build_store = lambda: (_ for _ in ()).throw(RuntimeError("qdrant down"))
    vs.reset_store()
    invalidate_retriever()
    from app.db.engine import session_scope

    with session_scope() as session:
        retriever = get_retriever(session)
        hits = retriever.search("清仓 折扣 阶梯", topk=3)
    vs.build_store = original_build
    vs.reset_store()
    invalidate_retriever()
    case("向量库不可用：检索降级为本地向量且仍有结果",
         bool(hits) and retriever.store is None, f"hits={len(hits)}")

    # ④ 重排超时：回退融合序 + 诚实标注
    async def _slow(query, candidates):  # noqa: ANN001, ANN202
        await asyncio.sleep(0.5)
        return []

    from app.rag import rerank as rerank_mod

    original_llm = rerank_mod._llm_rerank
    rerank_mod._llm_rerank = _slow
    from app.rag.pipeline import search_kb

    result = asyncio.run(search_kb("清仓 折扣 阶梯", topk=3, backend="llm", use_cache=False))
    rerank_mod._llm_rerank = original_llm
    case("重排超时：仍有结果且 rerank_degraded=True",
         bool(result.hits) and result.rerank.get("degraded") is True,
         result.rerank)

    # ⑤ LLM 失败：重排给出可读降级原因（不假装排过）
    async def _boom(query, candidates):  # noqa: ANN001, ANN202
        raise RuntimeError("llm 500")

    rerank_mod._llm_rerank = _boom
    failed = asyncio.run(search_kb("补货 安全库存", topk=2, backend="llm", use_cache=False))
    rerank_mod._llm_rerank = original_llm
    case("LLM 失败：重排降级并保留降级原因",
         failed.rerank.get("degraded") is True and failed.rerank.get("reason"),
         failed.rerank.get("reason"))

    # ⑥ Redis 不可用：会话锁 fail-closed
    settings.redis_url = "redis://127.0.0.1:6399/0"
    settings.redis_socket_connect_timeout = 0.2
    settings.redis_socket_timeout = 0.2
    from app.api import chat_routes
    from app.cache.redis_factory import reset_redis

    reset_redis()
    from fastapi import HTTPException

    async def _acquire():  # noqa: ANN202
        guard = chat_routes._SessionGuard("degradation-check")
        try:
            await guard.acquire()
            await guard.release()
            return None
        except HTTPException as exc:
            return exc

    exc = asyncio.run(_acquire())
    case("Redis 不可用：会话锁 fail-closed（503，不静默放行）",
         exc is not None and exc.status_code == 503, getattr(exc, "detail", None))
    settings.redis_url = ""
    reset_redis()

    platform_tools.set_platform_client(None)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="降级路径测评")
    parser.add_argument("--min-pass", type=float, default=1.0)
    parser.add_argument("--json", default="")
    args = parser.parse_args(argv)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="eval_degradation_"))
    settings = _setup(tmp)
    cases = _cases(settings)
    passed = sum(1 for c in cases if c["ok"])
    total = len(cases)
    ratio = passed / total if total else 0.0
    for item in cases:
        print(f"{'PASS' if item['ok'] else 'FAIL'}  {item['name']}"
              + (f"  -> {item['detail']}" if not item["ok"] else ""))
    print(f"\n降级路径：{passed}/{total} 通过（{ratio:.1%}）")
    if args.json:
        pathlib.Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.json).write_text(
            json.dumps({"passed": passed, "total": total, "ratio": ratio, "cases": cases},
                       ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ratio >= args.min_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
