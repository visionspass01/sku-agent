"""FastAPI 应用入口：API + 静态看板 + 生命周期（按 APP_MODULES 决定启用哪些 Agent）。

模块开关（`APP_MODULES`，默认 `sku,image`）：
- `sku`：SKU 监管 Agent（看板 / 对话 / 研判 / 巡检 / 知识库索引 / MCP hub / 定时任务）；
- `image`：电商生图 Agent（`/api/image/*`，独立账号 + 配额 + 生图链路）；
只启用 `image` 时不会初始化 SKU 的表与后台任务，首页给一句入口提示。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.agents import checkpoint as agent_checkpoint
from app.agents_image import runtime as image_runtime
from app.agents_image.api.routes import router as image_router
from app.api.chat_routes import router as chat_router
from app.api.routes import router
from app.config import BASE_DIR, STATIC_DIR, get_settings
from app.db.engine import init_schema, session_scope
from app.mcp_client.client import get_mcp_hub, shutdown_mcp_hub
from app.observability import latency
from app.rag.index import build_index
from app.scheduler import start_scheduler, stop_scheduler
from app.static_assets import render_index_html, render_page_html

logger = logging.getLogger("sku-agent")


def parse_modules(value: str | None = None) -> set[str]:
    """解析 `APP_MODULES`：逗号分隔，默认 sku,image。"""
    import os

    raw = (value if value is not None else os.environ.get("APP_MODULES")) or "sku,image"
    modules = {part.strip().lower() for part in str(raw).split(",") if part.strip()}
    return modules or {"sku", "image"}


MODULES = parse_modules()
SKU_ENABLED = "sku" in MODULES
IMAGE_ENABLED = "image" in MODULES


async def _sku_startup() -> None:
    """SKU 监管 Agent 的全部启动逻辑（APP_MODULES 不含 sku 时整段跳过）。"""
    settings = get_settings()
    init_schema()

    # 部署自检：公开默认密钥仍在使用时告警（容器部署 .env 放错位置会静默退回默认值）
    from app.config import DEFAULT_AUTH_SECRET, DEFAULT_PLATFORM_INTERNAL_TOKEN

    if settings.auth_secret == DEFAULT_AUTH_SECRET:
        logger.warning(
            "AUTH_SECRET 仍是仓库公开默认值：任何人可据此伪造 Token。"
            "对外部署请用 openssl rand -hex 32 生成并写入 .env（详见 docs/DEPLOY.md 第 3、9 节）"
        )
    if settings.platform_internal_token == DEFAULT_PLATFORM_INTERNAL_TOKEN:
        logger.warning(
            "PLATFORM_INTERNAL_TOKEN 仍是仓库公开默认值：能访问中台端口的人即可获得全权身份。"
            "请改为随机值，并确认中台侧同名变量一致"
        )

    with session_scope() as session:
        # 索引增量同步：按 sha256 只重建变动的文档；首次（空库）则全量构建
        try:
            stats = build_index(session)
            if stats.added or stats.updated or stats.deleted:
                logger.info("知识库索引已同步：%s", stats.to_dict())
        except Exception as exc:  # noqa: BLE001 —— 索引失败不该阻塞服务
            logger.warning("知识库索引同步失败：%s", exc)
        # 向量库（可选）**按文档指纹对账**：只重建不一致的文档，不再"数量不符就整库重建"
        try:
            from app.rag.index import ensure_store_synced

            logger.info("向量库对账：%s", ensure_store_synced(session))
        except Exception as exc:  # noqa: BLE001
            logger.warning("向量库对账失败（检索将降级为内存模式）：%s", exc)
        # 预热检索器：首次装载要读全部 chunk + 建 BM25，放到启动阶段，别让第一个用户等
        if getattr(settings, "rag_index_warmup", True):
            try:
                import time as _time

                from app.rag.hybrid import get_retriever

                started = _time.perf_counter()
                retriever = get_retriever(session)
                logger.info("检索器已预热：%d 块 / %d ms", len(retriever.rows),
                            int((_time.perf_counter() - started) * 1000))
            except Exception as exc:  # noqa: BLE001
                logger.warning("检索器预热失败：%s", exc)

    hub = get_mcp_hub()
    try:
        await hub.startup()
        logger.info("MCP hub 已启动（transport=%s）", settings.mcp_transport)
    except Exception as exc:  # 不因 MCP 启动失败阻塞服务，调用时会显式报错
        logger.warning("MCP hub 启动失败：%s", exc)

    # 分析线程 checkpointer（持久化）：SQLite 默认，失败只告警不阻塞启动
    try:
        await agent_checkpoint.startup()
    except Exception as exc:  # noqa: BLE001
        logger.warning("checkpointer 初始化失败（追问退化为进程内）：%s", exc)

    start_scheduler()

    # 档案主文档的「待合并」队列：启动时先清一批（上次进程退出前没来得及整合摘要）
    try:
        from app.memory import org_memory

        drained = await org_memory.adrain_pending_merges(limit=5)
        if drained.get("processed"):
            logger.info("启动清理待合并档案：%s", drained)
    except Exception as exc:  # noqa: BLE001 —— 清理失败不影响启动
        logger.warning("启动清理待合并档案失败：%s", exc)

    # 预热中台连接池（首次新建 httpx client 要 0.5~1.4s，放到启动阶段，别让用户等）
    try:
        from app.tools.platform_tools import get_platform_client

        await get_platform_client().get("/api/v1/health", params={"warmup": 1})
    except Exception:  # noqa: BLE001 —— 中台可能还没就绪，不影响启动
        pass

    # 部门 ↔ 品类映射以中台为准（离线时用 app/config.py 里的本地默认值兜底）
    # 顺序很重要：**先读磁盘缓存**（带指纹），再拉中台覆盖。否则中台短暂不可用时
    # 会悄悄退回硬编码默认值——部门隔离按错的品类生效，且没人会发现。
    try:
        from app.config import load_cached_department_categories

        cached = load_cached_department_categories()
        if cached.get("mapping"):
            logger.info("部门映射已从本地缓存恢复（version=%s，synced_at=%s）",
                        cached.get("version"), cached.get("synced_at"))
    except Exception as exc:  # noqa: BLE001
        logger.info("部门映射本地缓存读取跳过：%s", exc)

    try:
        from app.config import set_department_categories
        from app.tools.platform_tools import get_platform_client

        data = await get_platform_client().get("/api/v1/metadata/departments")
        mapping = (data or {}).get("departments") or {}
        if mapping:
            logger.info("部门映射已与中台同步：%s（version=%s）",
                        set_department_categories(mapping, str((data or {}).get("version") or "")),
                        (data or {}).get("version"))
        else:
            logger.info("中台未返回部门映射，沿用本地缓存/默认值（%s）",
                        (data or {}).get("error", "空响应"))
    except Exception as exc:  # noqa: BLE001 —— 同步失败不影响启动
        logger.info("部门映射同步跳过（沿用本地缓存/默认值）：%s", exc)

    # 能力目录 ↔ 中台对版本：漂移只告警 + 写进 /api/health.capabilities，不阻塞启动
    try:
        from app.tools.platform_tools import sync_capabilities

        cap_status = await sync_capabilities()
        logger.info("能力目录同步：%d 个工具 / %d 个分组（中台版本 %s，漂移 %d 项）",
                    cap_status.get("tools") or 0, cap_status.get("groups") or 0,
                    cap_status.get("remote_version") or "-", len(cap_status.get("drift") or []))
    except Exception as exc:  # noqa: BLE001
        logger.info("能力目录同步跳过：%s", exc)

    # SKU 参考目录：Agent 侧部门作用域二次判定用它（不再读 Agent 私有业务表）
    try:
        from app.tools import catalog as sku_catalog

        if settings.catalog_sync_on_startup:
            cat = await sku_catalog.sync()
            logger.info("SKU 参考目录：%s", {k: cat.get(k) for k in
                                             ("loaded", "skus", "styles", "data_version", "error")})
    except Exception as exc:  # noqa: BLE001
        logger.info("SKU 参考目录同步跳过：%s", exc)

    # RAG 同义词：业务知识在中台，Agent 启动时拉一次（失败用内置兜底表）
    try:
        from app.rag import synonyms as rag_synonyms

        status = await rag_synonyms.refresh(force=True)
        logger.info("RAG 同义词：%s", status)
    except Exception as exc:  # noqa: BLE001
        logger.info("RAG 同义词同步跳过：%s", exc)


async def _sku_shutdown() -> None:
    """SKU 监管 Agent 的关闭逻辑（与启动对称）。"""
    stop_scheduler()
    await agent_checkpoint.shutdown()
    await shutdown_mcp_hub()
    try:
        from app.tools.platform_tools import aclose_platform_client

        await aclose_platform_client()
    except Exception:  # noqa: BLE001 —— 关闭连接池失败不影响进程退出
        pass
    try:
        from app.agents.chat_agent import aclose_shared_http

        await aclose_shared_http()   # 对话模型复用的共享连接池
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.cache.redis_factory import aclose_async_redis

        # 异步 Redis 客户端（锁专用）也要关：否则 reload/测试里会留下未关闭的连接与
        # "coroutine never awaited" 告警，看起来像锁泄漏。
        await aclose_async_redis()
    except Exception:  # noqa: BLE001
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()

    # 日志注入 trace_id：一条用户提问产生的所有日志（工具/中台/MCP/RAG/LLM）可按 trace 串联
    from app.observability import trace as trace_mod

    trace_mod.install_logging()

    if SKU_ENABLED:
        await _sku_startup()
    if IMAGE_ENABLED:
        await image_runtime.startup()
    yield
    if IMAGE_ENABLED:
        await image_runtime.shutdown()
    if SKU_ENABLED:
        await _sku_shutdown()


app = FastAPI(title="电商 SKU 监管 Agent", version="0.1.0", lifespan=lifespan)
if SKU_ENABLED:
    app.include_router(router)
    app.include_router(chat_router)
if IMAGE_ENABLED:
    app.include_router(image_router)

if not SKU_ENABLED:
    from app.core import health as _health_registry

    @app.get("/api/health", include_in_schema=False)
    def health_without_sku() -> dict:
        """只启用 image 时也保留一个 `/api/health`（目录页 / 前端自检都靠它）。"""
        return {"status": "ok", "modules": sorted(MODULES), **_health_registry.collect()}

# 静态页面/脚本必须每次回源校验：否则部署后浏览器可能"HTML 是新的、app.js 还是旧的"，
# 导致新功能（运行详情 / 记忆面板 / 历史会话）函数不存在、bindEvents 中途抛错。
_NO_CACHE_SUFFIXES = (".html", ".js", ".css", ".md", ".json")
# 看板每 3s 轮询的轻量接口不计入 api 延迟，避免把 P50 拉成几毫秒的"假象"
_LATENCY_SKIP = {"/api/health", "/api/metrics", "/api/queue/stats", "/api/chat/stream"}

# 基础安全响应头（生产必备，成本≈0）：
# - nosniff / DENY / same-origin 挡住 MIME 嗅探、iframe 嵌套、Referer 泄露；
# - CSP 目前保留 'unsafe-inline'：index.html 里还有内联 <script> 与 style 属性，
#   去掉会白屏；但它已经能挡住「外链脚本被注入」这条路。想彻底收紧需要把内联脚本外置 +
#   token 迁到 HttpOnly Cookie（见 docs/DEPLOY.md 的生产清单）。
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    ),
}


@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    """静态资源不缓存 + 安全响应头 + 记录 /api/* 请求耗时（看板 P50/P95 采样）。"""
    import time

    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    path = request.url.path
    if path == "/" or path.endswith(_NO_CACHE_SUFFIXES):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if IMAGE_ENABLED and path.startswith("/api/image"):
        # 成功响应也回传 trace：用户报障时直接给 id 就能串起日志与任务（错误响应由 core.errors 写）
        if "X-Trace-Id" not in response.headers:
            import uuid

            response.headers["X-Trace-Id"] = (request.headers.get("X-Trace-Id")
                                              or uuid.uuid4().hex[:12])
    if path.startswith("/api/") and path not in _LATENCY_SKIP:
        latency.record("api", elapsed_ms)
    return response


# index.html 走路由渲染：给 app.js / style.css 追内容哈希（?v=xxx），从根上避免旧脚本
@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index_page() -> HTMLResponse:
    if not SKU_ENABLED:
        return HTMLResponse(
            "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\"/>"
            "<title>仅启用图像 Agent</title></head>"
            "<body style=\"font-family:system-ui;background:#0b1220;color:#dbeafe;"
            "display:flex;align-items:center;justify-content:center;height:100vh\">"
            "<div><h2>当前只启用了「电商生图 Agent」</h2>"
            "<p>请访问 <a style=\"color:#7dd3fc\" href=\"/agents.html\">/agents.html</a>"
            " 或 <a style=\"color:#7dd3fc\" href=\"/image.html\">/image.html</a>。</p>"
            "<p style=\"color:#94a3b8\">（启动参数：APP_MODULES=image）</p></div></body></html>"
        )
    return HTMLResponse(render_index_html())


# Agent 目录页（一页一 Agent 的并行结构）：新增 Agent 演示页时，只加一条 AGENT_PAGES 数据 +
# 一个新 HTML；目录页本身与 `index.html` 走同一条渲染路径，才能拿到带 ?v= 的脚本。
@app.get("/agents.html", include_in_schema=False)
def agents_page() -> HTMLResponse:
    return HTMLResponse(render_page_html("agents.html"))


# 项目简介：about.html 通过 fetch('/about.md') 渲染。文档放在 docs/ 而不是 static/，
# 前端资产目录只放真正的前端文件，文档也能在 GitHub 上直接阅读。
_ABOUT_DOC = BASE_DIR / "docs" / "ABOUT.md"


@app.get("/about.md", include_in_schema=False)
def about_doc() -> PlainTextResponse:
    try:
        return PlainTextResponse(_ABOUT_DOC.read_text(encoding="utf-8"),
                                 media_type="text/markdown; charset=utf-8")
    except OSError:
        return PlainTextResponse("# 项目简介\n\n文档加载失败（docs/ABOUT.md 缺失）。\n",
                                 media_type="text/markdown; charset=utf-8")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
